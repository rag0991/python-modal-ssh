import modal, subprocess, time, argparse
from pathlib import Path
from typing import List
from datetime import datetime
from tqdm import tqdm

# Expand the pubkey path on YOUR machine (client side). No existence check here.
pubkey_path = str(Path("~/.ssh/id_rsa.pub").expanduser())
# If you *really* want to support RSA fallback, do it entirely client-side:
# alt = Path("~/.ssh/id_rsa.pub").expanduser()
# if not Path(pubkey_path).exists() and alt.exists():
#     pubkey_path = str(alt)

# Create a placeholder app that will be replaced by dynamic app
app = modal.App()

@app.local_entrypoint()
def main(
    cpu: int = 0,              # optional; vCPU cores
    memory: int = 0,           # optional; GB
    gpu: str = "",            # optional; e.g., "H100", "H100:3", "A100-80GB"
    timeout: int = 0,          # optional; hours
    image: str = "",          # optional; e.g., "ubuntu:22.04"
    add_python: str = "",     # optional; python version when using debian_slim
    mount=None,                # optional; repeatable local mounts: local[:/remote]
    volume=None,               # optional; repeatable volume names
    detach: bool = False,      # optional; accepted for parity (handled by CLI)
):
    """
    Launch an SSH-enabled container with configurable GPU and volume options.

    Args:
        gpu: GPU configuration (e.g., "A100-40GB", "H100:2", "L40S", "any")
        volume: Volume names to mount (can be specified multiple times)

    Examples:
        modal run ssh.py --gpu H100
        modal run ssh.py --gpu "A100-80GB:2"
        modal run ssh.py --volume my-data --volume my-models
        modal run ssh.py --gpu L40S --volume my-volume
    """
    # Use the ephemeral app provided by 'modal run' to avoid creating 2 apps
    # This means we can't control the app name, but we only get 1 app
    current_time = datetime.now().strftime("%H%M")
    print(f"SSH session started at: {current_time}")
    
    # Build image dynamically based on flags
    if image:
        try:
            runtime_image = modal.Image.from_registry(image)
        except Exception:
            runtime_image = modal.Image.debian_slim(
                python_version=add_python if add_python else None
            )
    else:
        runtime_image = (
            modal.Image.debian_slim(
                python_version=add_python if add_python else None
            )
        )

    runtime_image = (
        runtime_image
        .apt_install("openssh-server")
        .run_commands(
            "mkdir -p /run/sshd /root/.ssh",
            "chmod 700 /root/.ssh",
            'echo "PasswordAuthentication no" >> /etc/ssh/sshd_config',
            'echo "PermitRootLogin prohibit-password" >> /etc/ssh/sshd_config',
        )
        .add_local_file(pubkey_path, "/root/.ssh/authorized_keys", copy=True)
        .run_commands("chmod 600 /root/.ssh/authorized_keys")
    )

    # If using a registry image and add_python provided, best-effort install python
    if image and add_python:
        try:
            runtime_image = runtime_image.apt_install(
                f"python{add_python}", "python3-pip"
            )
        except Exception:
            pass

    # Process mount arguments (local directory mounts)
    mounts_list = []
    if mount:
        mount_list = [mount] if isinstance(mount, str) else mount
        for spec in mount_list:
            if not spec:
                continue
            if ":" in spec:
                local, remote = spec.split(":", 1)
                remote_path = remote
            else:
                local = spec
                remote_path = f"/mounts/{Path(local).expanduser().name}"
            mounts_list.append(
                modal.Mount.from_local_dir(
                    str(Path(local).expanduser()), remote_path=remote_path
                )
            )

    # Process volume arguments - handle both string and list cases
    volume_config = {}
    if volume:
        # If volume is a string (single volume), convert to list
        if isinstance(volume, str):
            volume_list = [volume]
        else:
            volume_list = volume

        for vol_name in volume_list:
            if vol_name:  # Skip empty strings
                # Create or get existing volume
                vol = modal.Volume.from_name(vol_name, create_if_missing=True)
                # Mount at /vol/<volume_name>
                mount_path = f"/vol/{vol_name}"
                volume_config[mount_path] = vol
                print(f"Volume '{vol_name}' will be mounted at {mount_path}")

    print(f"Starting SSH container with GPU: {gpu or 'none'}")
    if volume_config:
        print(f"Mounting {len(volume_config)} volume(s)")

    # Launch via Sandbox to apply dynamic image/cpu/memory/gpu/volumes/mounts
    sb_kwargs = {
        "app": app,
        "image": runtime_image,
        "unencrypted_ports": [22],
    }
    if timeout and timeout > 0:
        # Convert hours -> seconds for Sandbox
        sb_kwargs["timeout"] = timeout * 60 * 60
    if gpu:
        sb_kwargs["gpu"] = gpu
    if volume_config:
        sb_kwargs["volumes"] = volume_config
    if cpu and cpu > 0:
        sb_kwargs["cpu"] = float(cpu)
    if memory and memory > 0:
        # Convert GB -> MiB for Sandbox
        sb_kwargs["memory"] = int(memory) * 1024
    if mounts_list:
        sb_kwargs["mounts"] = mounts_list

    sb = modal.Sandbox.create(**sb_kwargs)
    # Start sshd
    sb.exec("/usr/sbin/sshd", "-D", "-e")
    tunnel = sb.tunnels()[22]
    host, port = tunnel.tcp_socket
    print(f"SSH ready. Connect with:\n  ssh -p {port} root@{host}")
    try:
        subprocess.run(["bash", "-lc", "ls -lah /vol || true"], check=False)
    except Exception:
        pass

    # Keep alive until timeout, sandbox termination, or interrupted; --detach is handled by CLI
    start_time = time.time()
    timeout_seconds = timeout * 60 * 60 if timeout and timeout > 0 else None
    
    def format_time(seconds):
        """Convert seconds to HH:MM format"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours:02d}:{minutes:02d}"
    
    # Initialize progress bar if timeout is set
    pbar = None
    if timeout_seconds:
        timeout_minutes = timeout_seconds // 60
        elapsed_time = format_time(0)
        remaining_time = format_time(timeout_seconds)
        pbar = tqdm(
            total=timeout_minutes, 
            desc=f"SSH session ({timeout}h timeout) - Elapsed: {elapsed_time}, Remaining: {remaining_time}",
            unit="min",
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}min"
        )
        last_progress_update = start_time
    
    try:
        while True:
            current_time = time.time()
            elapsed = current_time - start_time
            
            # Check if sandbox is still running
            try:
                if sb.poll() is not None:
                    print("\nSandbox has terminated.")
                    break
            except Exception:
                # If poll fails, assume sandbox is terminated
                print("\nSandbox connection lost.")
                break
            
            # Check local timeout as backup mechanism
            if timeout_seconds:
                if elapsed >= timeout_seconds:
                    print(f"\nTimeout reached ({timeout} hours). Terminating session...")
                    sb.terminate()
                    try:
                        sb.wait(raise_on_termination=False)
                        print("Sandbox terminated due to timeout.")
                    except Exception as e:
                        print(f"Warning: Error during timeout cleanup: {e}")
                    break
                
                # Update progress bar every 10 minutes (600 seconds)
                if current_time - last_progress_update >= 600:
                    if pbar:
                        elapsed_minutes = int(elapsed // 60)
                        remaining_seconds = timeout_seconds - elapsed
                        elapsed_time = format_time(elapsed)
                        remaining_time = format_time(remaining_seconds)
                        
                        pbar.n = elapsed_minutes
                        pbar.set_description(f"SSH session ({timeout}h timeout) - Elapsed: {elapsed_time}, Remaining: {remaining_time}")
                        pbar.refresh()
                    last_progress_update = current_time
            
            time.sleep(30)  # Check more frequently for better responsiveness
            
    except KeyboardInterrupt:
        print("\nSession interrupted by user.")
        print("Terminating sandbox...")
        sb.terminate()
        # Wait for sandbox to fully terminate to ensure proper cleanup
        try:
            sb.wait(raise_on_termination=False)
            print("Sandbox terminated successfully.")
        except Exception as e:
            print(f"Warning: Error during sandbox cleanup: {e}")
    finally:
        # Ensure sandbox is terminated even if not caught by KeyboardInterrupt
        try:
            if sb.poll() is None:  # Check if sandbox is still running
                print("Ensuring sandbox cleanup...")
                sb.terminate()
                sb.wait(raise_on_termination=False)
        except Exception as e:
            print(f"Warning: Error during final cleanup: {e}")
        
        # Clean up progress bar
        if pbar:
            pbar.close()
