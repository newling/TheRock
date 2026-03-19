#!/usr/bin/env python3
"""
CI Telemetry Collector - Standalone telemetry collection for GitHub Actions

Collects CPU/memory metrics during test execution and writes them in Prometheus format.
Designed to work with existing self-hosted runners (not orchestrator-managed).

Usage:
    # Start collection before test step
    python ci_telemetry_collector.py start --output-dir ./metrics

    # Stop collection after test step
    python ci_telemetry_collector.py stop

Features:
- Samples container CPU/memory usage at configurable intervals
- Writes metrics in Prometheus format for compatibility with orchestrator dashboards
- Handles graceful shutdown on SIGTERM/SIGINT
- Runs as background process to not block test execution

Environment Variables (GitHub Actions provides these automatically):
- GITHUB_REPOSITORY: Repository name
- GITHUB_WORKFLOW: Workflow name
- GITHUB_JOB: Job name
- GITHUB_RUN_ID: Run ID
- GITHUB_RUN_ATTEMPT: Run attempt number
- RUNNER_NAME: Runner name
- CONTAINER_ID: Container ID (must be set by workflow)
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

# Default configuration
DEFAULT_SAMPLING_INTERVAL = 5  # seconds
DEFAULT_FLUSH_INTERVAL = 30  # seconds
STATE_FILE = "/tmp/ci_telemetry_state.json"


class TelemetryCollector:
    """Collects and writes telemetry metrics in Prometheus format"""

    def __init__(
        self,
        container_id: str,
        output_file: Path,
        current_file: Path,
        labels: Dict[str, str],
        sampling_interval: int = DEFAULT_SAMPLING_INTERVAL,
        flush_interval: int = DEFAULT_FLUSH_INTERVAL,
    ):
        self.container_id = container_id
        self.output_file = output_file
        self.current_file = current_file
        self.labels = labels
        self.sampling_interval = sampling_interval
        self.flush_interval = flush_interval
        self.sample_buffer = []
        self.start_time = time.time()
        self.running = True
        self.last_flush_time = time.time()

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully"""
        print(f"[telemetry] Received signal {signum}, flushing remaining samples...")
        self.running = False

    def _get_container_stats(self) -> Optional[Dict[str, float]]:
        """Get CPU and memory stats from container"""
        try:
            cmd = [
                "docker",
                "stats",
                "--no-stream",
                "--format",
                "{{.CPUPerc}},{{.MemUsage}}",
                self.container_id,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5, check=True
            )
            output = result.stdout.strip()

            if not output:
                return None

            parts = output.split(",")
            if len(parts) != 2:
                return None

            # Parse CPU percentage (e.g., "25.5%" -> 25.5)
            cpu_str = parts[0].strip().rstrip("%")
            cpu_percent = float(cpu_str) if cpu_str else 0.0

            # Parse memory (e.g., "1.5GiB / 32GiB" -> 1610612736 bytes)
            mem_str = parts[1].strip().split("/")[0].strip()
            mem_bytes = self._parse_memory_bytes(mem_str)

            return {"cpu_percent": cpu_percent, "memory_bytes": mem_bytes}

        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError) as e:
            print(f"[telemetry] Error collecting stats: {e}", file=sys.stderr)
            return None

    def _parse_memory_bytes(self, mem_str: str) -> float:
        """Parse memory string to bytes (e.g., '1.5GiB' -> bytes)"""
        mem_str = mem_str.strip()
        multipliers = {
            "B": 1,
            "KiB": 1024,
            "MiB": 1024**2,
            "GiB": 1024**3,
            "TiB": 1024**4,
            "KB": 1000,
            "MB": 1000**2,
            "GB": 1000**3,
            "TB": 1000**4,
        }

        # Match number and unit
        match = re.match(r"([0-9.]+)\s*([A-Za-z]+)?", mem_str)
        if not match:
            return 0.0

        value = float(match.group(1))
        unit = match.group(2) or "B"

        return value * multipliers.get(unit, 1)

    def _format_labels(self, extra_labels: Optional[Dict[str, str]] = None) -> str:
        """Format labels for Prometheus metric"""
        labels = self.labels.copy()
        if extra_labels:
            labels.update(extra_labels)

        return ",".join([f'{k}="{v}"' for k, v in labels.items()])

    def _flush_samples(self):
        """Write buffered samples to disk"""
        if not self.sample_buffer:
            return

        try:
            # Write accumulated samples to main metrics file (append)
            with open(self.output_file, "a") as f:
                for sample in self.sample_buffer:
                    f.write(sample + "\n")

            print(
                f"[telemetry] Flushed {len(self.sample_buffer)} samples to {self.output_file}"
            )
            self.sample_buffer.clear()
            self.last_flush_time = time.time()

        except OSError as e:
            print(f"[telemetry] Error writing metrics: {e}", file=sys.stderr)

    def _write_current_metrics(self, stats: Dict[str, float], elapsed: float):
        """Write current metrics (overwritten each sample for real-time aggregation)"""
        try:
            label_str = self._format_labels()
            lines = [
                f"step_running{{{label_str}}} {elapsed:.1f}",
                f"runner_cpu_current{{{label_str}}} {stats['cpu_percent']:.2f}",
                f"runner_memory_current{{{label_str}}} {stats['memory_bytes']:.0f}",
            ]

            with open(self.current_file, "w") as f:
                f.write("\n".join(lines) + "\n")

        except OSError as e:
            print(f"[telemetry] Error writing current metrics: {e}", file=sys.stderr)

    def collect_sample(self):
        """Collect a single sample"""
        stats = self._get_container_stats()
        if not stats:
            return

        elapsed = time.time() - self.start_time
        timestamp = int(time.time())

        # Build Prometheus metric lines
        label_str = self._format_labels()
        cpu_line = f"step_cpu_percent{{{label_str}}} {stats['cpu_percent']:.2f} {timestamp}000"
        mem_line = f"step_memory_bytes{{{label_str}}} {stats['memory_bytes']:.0f} {timestamp}000"

        # Add to buffer
        self.sample_buffer.append(cpu_line)
        self.sample_buffer.append(mem_line)

        # Write current metrics (overwritten each time)
        self._write_current_metrics(stats, elapsed)

        # Flush if interval elapsed
        if time.time() - self.last_flush_time >= self.flush_interval:
            self._flush_samples()

    def run(self):
        """Main collection loop"""
        print(
            f"[telemetry] Starting collection for container {self.container_id[:12]}"
        )
        print(
            f"[telemetry] Sampling interval: {self.sampling_interval}s, flush interval: {self.flush_interval}s"
        )

        while self.running:
            self.collect_sample()
            time.sleep(self.sampling_interval)

        # Final flush on shutdown
        self._flush_samples()
        print("[telemetry] Collector stopped")

    def write_final_metrics(self, duration: float, exit_code: int, status: str):
        """Write final step metrics (duration and exit code)"""
        try:
            label_str = self._format_labels({"status": status})
            lines = [
                f"step_duration_seconds{{{label_str}}} {duration:.3f}",
                f"step_exit_code{{{self._format_labels()}}} {exit_code}",
            ]

            with open(self.output_file, "a") as f:
                f.write("\n".join(lines) + "\n")

            # Update current metrics file with final duration
            with open(self.current_file, "w") as f:
                f.write(lines[0] + "\n")

            print(f"[telemetry] Wrote final metrics (duration={duration:.2f}s, exit={exit_code})")

        except OSError as e:
            print(f"[telemetry] Error writing final metrics: {e}", file=sys.stderr)


def get_labels() -> Dict[str, str]:
    """Extract labels from environment variables"""
    step_name = os.getenv("TELEMETRY_STEP_NAME", os.getenv("GITHUB_JOB", "unknown"))
    step_id = os.getenv("TELEMETRY_STEP_ID", "test-step")

    return {
        "repo": os.getenv("GITHUB_REPOSITORY", "unknown"),
        "branch": os.getenv("GITHUB_REF_NAME", os.getenv("GITHUB_HEAD_REF", "unknown")),
        "workflow": os.getenv("GITHUB_WORKFLOW", "unknown"),
        "github_job": os.getenv("GITHUB_JOB", "unknown"),
        "step_id": step_id,
        "step_name": step_name,
        "step_uuid": "",  # Not available without orchestrator hooks
        "composite_action": "",
        "runner": os.getenv("RUNNER_NAME", "unknown"),
        "run_id": os.getenv("GITHUB_RUN_ID", "unknown"),
        "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", "1"),
        "matrix_index": "",
        "execution_time": str(int(time.time())),
    }


def start_collector(args):
    """Start telemetry collection in background"""
    container_id = os.getenv("HOSTNAME")  # In container jobs, HOSTNAME is the container ID
    if not container_id:
        print("Error: HOSTNAME not set (are you running in a container job?)", file=sys.stderr)
        sys.exit(1)

    # Setup output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate filenames
    runner_name = os.getenv("RUNNER_NAME", "unknown")
    run_id = os.getenv("GITHUB_RUN_ID", "unknown")
    run_attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1")
    output_file = output_dir / f"step-metrics-{runner_name}.prom"
    current_file = output_dir / f"runner-current-{runner_name}-{run_id}-{run_attempt}.prom"

    # Get labels
    labels = get_labels()

    # Save state for stop command
    state = {
        "pid": os.getpid(),
        "container_id": container_id,
        "output_file": str(output_file),
        "current_file": str(current_file),
        "labels": labels,
        "start_time": time.time(),
    }

    # Fork background process
    pid = os.fork()
    if pid > 0:
        # Parent process - save state and exit
        state["pid"] = pid
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
        print(f"[telemetry] Started collector (PID {pid})")
        print(f"[telemetry] Metrics will be written to {output_file}")
        return

    # Child process - run collector
    try:
        # Close inherited file descriptors
        sys.stdin.close()
        sys.stdout.close()
        sys.stderr = open("/tmp/telemetry_collector.log", "a")

        collector = TelemetryCollector(
            container_id=container_id,
            output_file=output_file,
            current_file=current_file,
            labels=labels,
            sampling_interval=args.sampling_interval,
            flush_interval=args.flush_interval,
        )
        collector.run()
    except Exception as e:
        print(f"[telemetry] Collector error: {e}", file=sys.stderr)
    finally:
        sys.exit(0)


def stop_collector(args):
    """Stop telemetry collection"""
    if not Path(STATE_FILE).exists():
        print("[telemetry] No running collector found")
        return

    try:
        with open(STATE_FILE) as f:
            state = json.load(f)

        pid = state["pid"]
        print(f"[telemetry] Stopping collector (PID {pid})")

        # Send SIGTERM to gracefully stop
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)  # Give it time to flush

            # Write final metrics
            duration = time.time() - state["start_time"]
            exit_code = args.exit_code
            status = "success" if exit_code == 0 else "failure"

            # Reconstruct collector to write final metrics
            labels = state["labels"]
            output_file = Path(state["output_file"])
            current_file = Path(state["current_file"])

            # Quick write of final metrics (collector already flushed samples)
            label_str = ",".join([f'{k}="{v}"' for k, v in labels.items()])
            final_label_str = label_str + f',status="{status}"'

            with open(output_file, "a") as f:
                f.write(f"step_duration_seconds{{{final_label_str}}} {duration:.3f}\n")
                f.write(f"step_exit_code{{{label_str}}} {exit_code}\n")

            with open(current_file, "w") as f:
                f.write(f"step_duration_seconds{{{final_label_str}}} {duration:.3f}\n")

            print(f"[telemetry] Wrote final metrics (duration={duration:.2f}s, exit={exit_code})")

        except ProcessLookupError:
            print("[telemetry] Collector already stopped")

        # Cleanup state file
        Path(STATE_FILE).unlink(missing_ok=True)

    except Exception as e:
        print(f"[telemetry] Error stopping collector: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="CI Telemetry Collector")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Start command
    start_parser = subparsers.add_parser("start", help="Start telemetry collection")
    start_parser.add_argument(
        "--output-dir",
        default="/tmp/ci-telemetry",
        help="Directory to write metrics files (default: /tmp/ci-telemetry)",
    )
    start_parser.add_argument(
        "--sampling-interval",
        type=int,
        default=DEFAULT_SAMPLING_INTERVAL,
        help=f"Sampling interval in seconds (default: {DEFAULT_SAMPLING_INTERVAL})",
    )
    start_parser.add_argument(
        "--flush-interval",
        type=int,
        default=DEFAULT_FLUSH_INTERVAL,
        help=f"Flush interval in seconds (default: {DEFAULT_FLUSH_INTERVAL})",
    )

    # Stop command
    stop_parser = subparsers.add_parser("stop", help="Stop telemetry collection")
    stop_parser.add_argument(
        "--exit-code",
        type=int,
        default=0,
        help="Exit code of the test step (default: 0)",
    )

    args = parser.parse_args()

    if args.command == "start":
        start_collector(args)
    elif args.command == "stop":
        stop_collector(args)


if __name__ == "__main__":
    main()
