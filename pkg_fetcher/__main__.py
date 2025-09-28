from __future__ import annotations

import argparse
import time
import sys
import tarfile
import shutil

from pathlib import Path
from typing import List, Optional
from pkg_fetcher.remote_deb_fetcher import RemoteDebFetcher
from pkg_fetcher.sshsession import SSHSession
from pkg_fetcher.utils import ToolError, info, warn, err

def run(
    host: str,
    user: str,
    package: str,
    out_dir: Path,
    *,
    skip_packages: Optional[List[str]] = None,
    port: int = 22,
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

                if skip_packages:
                    info(f"Skipping packages: {skip_packages}")
                    pkgs = [p for p in pkgs if p not in skip_packages]

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
    p.add_argument("--skips", type=str, help="Package names to skip during dependency resolution.")
    p.add_argument("--yes", action="store_true", help="Auto-continue on warnings when possible.")
    p.add_argument("--verbose", action="store_true", help="Verbose logs.")
    p.add_argument("package", help="Target Debian package name.")
    return p


def ensure_out_dir(path_str: str) -> Path:
    p = Path(path_str)
    if p.exists():
        if not p.is_dir():
            raise ToolError(f"Output path {p} exists and is not a directory.")
        if any(p.iterdir()):
            raise ToolError(f"Output directory {p} is not empty.")
    else:
        try:
            p.mkdir(parents=True, exist_ok=False)
        except Exception as e:
            raise ToolError(f"Failed to create output directory {p}: {e}") from e
    return p


def archive_output_dir(out_dir: Path) -> None:
    # Archive the output directory into a .tar.xz file using LZMA compression
    tar_path = out_dir.parent / f"{out_dir.name}.tar.xz"
    info(f"Archiving output directory to {tar_path} ...")

    if tar_path.exists():
        warn(f"Archive {tar_path} already exists. Overwriting.")
        tar_path.unlink()
    try:
        # Open a tarfile in xz compression mode
        with tarfile.open(tar_path, "w:xz") as tar:
            tar.add(out_dir, arcname=out_dir.name)
        info(f"Archive created: {tar_path}")
    except Exception as e:
        warn(f"Failed to create archive {tar_path}: {e}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    out_dir = ensure_out_dir(f"{args.package}-deb")
    try:
        run(
            host=args.host,
            user=args.user,
            port=args.port,
            out_dir=out_dir,
            package=args.package,
            skip_packages=args.skips.split(",") if args.skips else None,
            method=args.method,
            yes=args.yes,
            verbose=args.verbose,
        )

        archive_output_dir(out_dir)
    except ToolError as e:
        err(str(e))
        sys.exit(2)
    except KeyboardInterrupt:
        err("Interrupted by user.")
        sys.exit(130)

    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
