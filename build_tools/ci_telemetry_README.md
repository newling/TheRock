# CI Telemetry Integration

This directory contains the CI telemetry collector, which collects CPU and memory metrics during test execution in a format compatible with actions-orchestrator dashboards.

## Overview

The `ci_telemetry_collector.py` script collects telemetry data from test runs and writes it in Prometheus format. This allows you to:

1. **Monitor resource usage** during tests (CPU/memory)
2. **Identify bottlenecks** and resource-intensive test components
3. **Track test duration** and success/failure rates
4. **Use orchestrator Grafana dashboards** to visualize the data

## How It Works

The telemetry collector:

1. **Starts before test execution** - Launches a background process
2. **Samples container stats** - Collects CPU/memory every 5 seconds (configurable)
3. **Flushes to disk periodically** - Writes metrics every 30 seconds to survive cancellations
4. **Stops after test execution** - Writes final metrics (duration, exit code)
5. **Uploads metrics as artifacts** - Available for download and analysis

## Integration in test_component.yml

The telemetry integration has been added to `.github/workflows/test_component.yml`:

```yaml
# Before Test step
- name: Start telemetry collection
  if: ${{ inputs.platform == 'linux' }}
  env:
    TELEMETRY_STEP_NAME: "Test ${{ fromJSON(inputs.component).job_name }}"
    TELEMETRY_STEP_ID: "test-component"
  run: |
    chmod +x ./build_tools/ci_telemetry_collector.py
    python3 ./build_tools/ci_telemetry_collector.py start \
      --output-dir /tmp/ci-telemetry \
      --sampling-interval 5 \
      --flush-interval 30

# Test step runs here...

# After Test step
- name: Stop telemetry collection
  if: ${{ always() && inputs.platform == 'linux' }}
  run: |
    EXIT_CODE=${{ steps.test.outcome == 'success' && '0' || '1' }}
    python3 ./build_tools/ci_telemetry_collector.py stop --exit-code $EXIT_CODE

# Upload metrics as artifacts
- name: Upload telemetry metrics
  if: ${{ always() && inputs.platform == 'linux' }}
  uses: actions/upload-artifact@v6
  with:
    name: telemetry-metrics-${{ fromJSON(inputs.component).job_name }}-...
    path: /tmp/ci-telemetry/*.prom
    retention-days: 7
```

## Metrics Collected

The collector writes metrics in Prometheus format:

### Per-Sample Metrics (time-series)

- `step_cpu_percent{...}` - CPU usage percentage (sampled every 5s)
- `step_memory_bytes{...}` - Memory usage in bytes (sampled every 5s)

### Final Metrics (written on completion)

- `step_duration_seconds{...,status="success|failure"}` - Total test duration
- `step_exit_code{...}` - Test exit code (0=success, non-zero=failure)

### Current Metrics (real-time, overwritten)

- `step_running{...}` - Elapsed seconds (while running)
- `runner_cpu_current{...}` - Current CPU (for aggregation)
- `runner_memory_current{...}` - Current memory (for aggregation)

### Metric Labels

All metrics include these labels for filtering:

```
repo="ROCm/TheRock"
branch="main"
workflow="CI Nightly"
github_job="test_linux_artifacts"
step_id="test-component"
step_name="Test rocBLAS"
runner="runner-gpu0-123"
run_id="123456789"
run_attempt="1"
execution_time="1234567890"
```

## Downloading and Analyzing Metrics

### Download from GitHub Actions UI

1. Go to your workflow run
2. Scroll to "Artifacts" section at the bottom
3. Download `telemetry-metrics-<component>-shard-<N>-<family>.zip`
4. Extract to get `.prom` files

### Analyze with Prometheus

If you have Prometheus installed:

```bash
# Download and extract metrics
unzip telemetry-metrics-rocblas-shard-1-gfx942.zip
cd telemetry-metrics-*

# Query metrics with promtool
promtool query instant prometheus.yml 'step_cpu_percent{step_name="Test rocBLAS"}'
promtool query range prometheus.yml 'step_memory_bytes{step_name="Test rocBLAS"}' --start 1h --end now
```

### View with actions-orchestrator Grafana

If you have actions-orchestrator deployed with observability:

1. **Copy metrics to orchestrator metrics directory:**

   ```bash
   # On your orchestrator host
   cp *.prom /tmp/runner-controller/metrics/
   ```

2. **Open Grafana dashboard:**

   - Navigate to `http://<orchestrator-host>:3000`
   - Login (default: admin/admin)
   - Open "GitHub Actions Runners" dashboard
   - Metrics will appear in the time-series graphs

3. **View graphs:**
   - CPU usage over time per step
   - Memory usage over time per step
   - Step duration and status
   - Runner utilization

### Manual Analysis with Python

```python
#!/usr/bin/env python3
import re
from pathlib import Path

# Parse Prometheus metrics
def parse_prom_file(file_path):
    metrics = []
    with open(file_path) as f:
        for line in f:
            if line.startswith('#') or not line.strip():
                continue
            # Parse: metric_name{labels} value [timestamp]
            match = re.match(r'([a-z_]+)\{([^}]+)\}\s+([0-9.]+)(?:\s+([0-9]+))?', line)
            if match:
                metrics.append({
                    'metric': match.group(1),
                    'labels': dict(re.findall(r'(\w+)="([^"]*)"', match.group(2))),
                    'value': float(match.group(3)),
                    'timestamp': int(match.group(4)) if match.group(4) else None
                })
    return metrics

# Load metrics
metrics = parse_prom_file('step-metrics-runner-gpu0.prom')

# Filter CPU metrics
cpu_metrics = [m for m in metrics if m['metric'] == 'step_cpu_percent']
print(f"CPU samples: {len(cpu_metrics)}")
print(f"Avg CPU: {sum(m['value'] for m in cpu_metrics) / len(cpu_metrics):.2f}%")
print(f"Max CPU: {max(m['value'] for m in cpu_metrics):.2f}%")

# Find duration
duration_metrics = [m for m in metrics if m['metric'] == 'step_duration_seconds']
for m in duration_metrics:
    print(f"Duration: {m['value']:.2f}s (status: {m['labels'].get('status', 'unknown')})")
```

## Configuration

### Environment Variables (Optional)

You can customize telemetry collection via environment variables:

```yaml
- name: Start telemetry collection
  env:
    # Custom step name in metrics
    TELEMETRY_STEP_NAME: "My Custom Test"

    # Custom step ID
    TELEMETRY_STEP_ID: "custom-test-id"
  run: |
    python3 ./build_tools/ci_telemetry_collector.py start \
      --output-dir /tmp/ci-telemetry \
      --sampling-interval 10 \     # Sample every 10 seconds
      --flush-interval 60           # Flush every 60 seconds
```

### Command-Line Options

```bash
# Start with custom intervals
python3 ci_telemetry_collector.py start \
  --output-dir /custom/path \
  --sampling-interval 10 \    # Seconds between samples (default: 5)
  --flush-interval 60         # Seconds between disk flushes (default: 30)

# Stop with custom exit code
python3 ci_telemetry_collector.py stop \
  --exit-code 1               # Exit code of test (default: 0)
```

## Troubleshooting

### Collector doesn't start

**Symptom:** "Error: HOSTNAME not set"

**Solution:** The collector requires running inside a container job (uses `HOSTNAME` as container ID). Ensure you're using `container:` in your workflow.

### No metrics generated

**Symptom:** No `.prom` files in output directory

**Causes:**
1. Collector didn't start (check logs: `cat /tmp/telemetry_collector.log`)
2. Docker not accessible (check `docker ps` works in container)
3. Permissions issue (ensure `/tmp/ci-telemetry` is writable)

**Debug:**
```bash
# Check if collector is running
ps aux | grep ci_telemetry_collector

# Check logs
cat /tmp/telemetry_collector.log

# Verify docker access
docker ps

# Check output directory
ls -la /tmp/ci-telemetry/
```

### Metrics not uploaded

**Symptom:** Artifact upload fails or is empty

**Solution:**
- Ensure `if: ${{ always() }}` is set on upload step
- Check path `/tmp/ci-telemetry/*.prom` has files
- Verify retention-days is set

## Future Enhancements

When you migrate to orchestrator-managed runners, you'll get:

1. **Automatic telemetry injection** - No manual start/stop needed
2. **GPU metrics** - GPU utilization, memory, temperature, power
3. **Network metrics** - Bytes sent/received
4. **Retry logic** - Automatic retry on flaky tests
5. **Real-time dashboards** - Live updates in Grafana
6. **No-output timeouts** - Detect hanging tests
7. **Better isolation** - Resource limits per runner profile

## Related Files

- `ci_telemetry_collector.py` - Main collector script
- `.github/workflows/test_component.yml` - Integration point
- `/tmp/ci-telemetry/` - Metrics output directory (on runner)
- `/tmp/telemetry_collector.log` - Collector debug log

## References

- [actions-orchestrator](https://github.com/ROCm/actions-orchestrator) - Full orchestrator system
- [Prometheus Exposition Format](https://prometheus.io/docs/instrumenting/exposition_formats/)
- [Grafana Dashboards](https://grafana.com/docs/grafana/latest/dashboards/)
