#!/usr/bin/env python3
"""Run all new tests and report results."""
import subprocess
import sys
import os

os.chdir('D:/lamix')

test_files = [
    'tests/test_platform_manager.py',
    'tests/test_feishu_adapter.py',
    'tests/test_session_manager.py',
    'tests/test_tools.py',
    'tests/test_heartbeat.py',
    'tests/test_watchdog.py',
    'tests/test_session_store.py',
    'tests/test_session_search.py',
    'tests/test_planning_steps.py',
    'tests/test_safe_mode.py',
    'tests/test_self_update.py',
    'tests/test_cli.py',
]

total_passed = 0
total_failed = 0
total_skipped = 0

for tf in test_files:
    print(f"\n{'='*60}")
    print(f"Running {tf}...")
    print('='*60)
    
    result = subprocess.run(
        ['python3', '-m', 'pytest', tf, '-v', '--tb=line'],
        capture_output=True,
        text=True,
    )
    
    # 解析结果
    output = result.stdout + result.stderr
    
    # 统计
    if 'passed' in output or 'PASSED' in output:
        lines = output.split('\n')
        for line in lines:
            if 'passed' in line.lower() and '=' in line:
                print(line)
                break
    
    if result.returncode != 0:
        print("FAILED")
        total_failed += 1
        # 显示错误摘要
        lines = output.split('\n')
        for line in lines:
            if 'FAILED' in line or 'ERROR' in line or 'short test summary' in line:
                print(line)
    else:
        print("PASSED")
        total_passed += 1

print(f"\n{'='*60}")
print(f"SUMMARY")
print('='*60)
print(f"Total test files: {len(test_files)}")
print(f"Passed: {total_passed}")
print(f"Failed: {total_failed}")
print(f"Skipped: {total_skipped}")
