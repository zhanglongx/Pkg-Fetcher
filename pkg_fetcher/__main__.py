from __future__ import annotations

import argparse
import time
import sys
from pathlib import Path
from pkg_fetcher.remote_deb_fetcher import RemoteDebFetcher
from pkg_fetcher.sshsession import SSHSession
from pkg_fetcher.utils import ToolError, info, warn, err

def run(
    host: str,
    user: str,
    package: str,
    port: int = 22,
    out_dir: Path = Path("./deb_pkgs"),
    method: str = "auto",
    yes: bool = False,
    verbose: bool = False,
) -> None:
    """
    Orchestrate the whole flow:
    - Connect (keys preferred; fallback to password)
    - Ensure apt host, create remote temp dir
    - Resolve dependencies and download .deb into that dir
    - SFTP copy .deb back to A
    - Cleanup remote dir
    """
    t0 = time.time()
    out_dir.mkdir(parents=True, exist_ok=True)

    ssh = SSHSession(hostname=host, username=user, port=port, verbose=verbose)
    ssh.connect()
    fetcher = RemoteDebFetcher(ssh, non_interactive_yes=yes, verbose=verbose)

    try:
        fetcher.assert_apt_host()
        remote_dir = fetcher.mktemp_dir()
        used_strategy = ""

        try:
            if method in ("auto", "uris"):
                # Preferred strategy: apt-get --print-uris (full closure)
                uris = fetcher.compute_uris_via_apt_print(package)
                if not uris and method == "auto":
                    # fallback if empty set
                    raise ToolError("No URIs from print-uris; falling back.")
                fetcher.download_uris(uris, remote_dir)
                used_strategy = "print-uris"
            else:
                raise ToolError("Skip print-uris per method selection.")

        except Exception as primary_err:
            if method == "uris":
                raise
            warn(f"Primary strategy failed: {primary_err}")

            # Secondary: apt-rdepends + apt-get download
            try:
                if fetcher.ensure_apt_rdepends():
                    pkgs = fetcher.compute_packages_via_rdepends(package)
                else:
                    warn("apt-rdepends not found. Falling back to apt-cache depends.")
                    pkgs = fetcher.compute_packages_via_apt_cache(package)

                if not pkgs:
                    raise ToolError("Dependency expansion returned empty list.")

                fetcher.download_packages(pkgs, remote_dir)
                used_strategy = "apt-(r)depends + apt-get download"

            except Exception as secondary_err:
                # Re-raise with combined context
                raise ToolError(
                    f"Both strategies failed. Primary: {primary_err} | Secondary: {secondary_err}"
                )

        # List final files
        deb_files = fetcher.list_debs(remote_dir)
        if not deb_files:
            raise ToolError("No .deb files found after download. Aborting.")

        # Copy back to local
        info(f"Copying {len(deb_files)} .deb file(s) to {out_dir} ...")
        for rp in deb_files:
            lp = out_dir / Path(rp).name
            ssh.sftp_get(rp, lp)

        info(f"Copied: {len(deb_files)} file(s). Cleaning up remote cache ...")
        fetcher.cleanup_dir(remote_dir)
        elapsed = time.time() - t0
        info(f"Done. Strategy: {used_strategy}. Elapsed: {elapsed:.1f}s")
        info(f"Local output: {out_dir.resolve()}")

    finally:
        ssh.close()


# ---------------------------
# CLI
# ---------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Fetch a Debian package and all required .deb dependencies from a remote host via SSH."
    )
    p.add_argument("--host", required=True, help="Remote host/IP (computer B).")
    p.add_argument("-u", "--user", required=True, help="SSH username for remote host.")
    p.add_argument("-p", "--port", type=int, default=22, help="SSH port (default: 22).")
    p.add_argument("-o", "--out", default="./deb_pkgs", help="Local output directory on computer A.")
    p.add_argument(
        "--method",
        choices=["auto", "uris", "rdepends"],
        default="auto",
        help="Dependency resolution method. "
             "'auto' tries apt print-uris first, then falls back. "
             "'uris' forces print-uris only. "
             "'rdepends' forces apt-(r)depends + apt-get download.",
    )
    p.add_argument("--yes", action="store_true", help="Auto-continue on warnings when possible.")
    p.add_argument("--verbose", action="store_true", help="Verbose logs.")
    p.add_argument("package", help="Target Debian package name.")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    out_dir = Path(args.out)
    try:
        run(
            host=args.host,
            user=args.user,
            port=args.port,
            package=args.package,
            out_dir=out_dir,
            method=args.method,
            yes=args.yes,
            verbose=args.verbose,
        )
    except ToolError as e:
        err(str(e))
        sys.exit(2)
    except KeyboardInterrupt:
        err("Interrupted by user.")
        sys.exit(130)


if __name__ == "__main__":
    main()
