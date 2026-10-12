"""Read CPU capacity from the daemon selected by the caller's Docker context."""
import json
import subprocess


def available_cpus():
    try:
        result = subprocess.run(['docker', 'info', '--format', '{{json .NCPU}}'],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError('Cannot read Docker daemon CPU capacity; check Docker access and context with bin/cave doctor') from error
    if result.returncode:
        raise RuntimeError('Cannot read Docker daemon CPU capacity; check Docker access and context with bin/cave doctor')
    try:
        capacity = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise RuntimeError('Docker daemon CPU capacity must be a positive integer') from error
    if type(capacity) is not int or capacity < 1:
        raise RuntimeError('Docker daemon CPU capacity must be a positive integer')
    return capacity
