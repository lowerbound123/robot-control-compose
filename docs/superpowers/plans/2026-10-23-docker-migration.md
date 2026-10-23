# Docker migration implementation plan

Approved by the user on 2026-10-23. Source: robot-control HEAD 2f25556 plus the complete current working tree, including runtime pause and snapshots. Original source remains unchanged.

## Constraints and design

- Independent Git repository; copy RC, SDK, only hold policy, applicable regression tests and ROS simulator. Preserve and track supplied arx5_arm_msg and vendor/arx_x5_ws including vendor assets; exclude build/runtime outputs.
- RC: Ubuntu22.04/ROS Humble/Python3.10/NumPy1.26.4. Policy: Python3.14 standard GIL/NumPy2.5.4. Shared library: posix-ipc1.3.2 with mmap and NumPy. Install dependencies inside images, no Conda.
- Compose: ipc-anchor owns shareable 512MiB /dev/shm; policy-hold and robot-control join it. Named volume /run/robot-control for UDS. No shared PID namespace or Docker socket. Mock+hold default, paused; ROS simulator profile and Linux ACOne host-network overlay. TUI attach, headless, persistent config/output/snapshots.
- Config schema2: policy_host replaces conda_env, policy_hosts maps hosts to sockets. Protocol is bounded length-prefixed JSON (4MiB) carrying state/actions/config/identities/descriptors, never image bytes.
- Each host has supervisor + one runtime process containing all policies. Independent module packages, contexts/configs and inference queues; helpers use relative imports. Python3.14 host preflights source without importing it; RC3.10 only checks files/manifest.
- Native chain A/B/A initializes A/B once; all stages read the same frames. Lifecycle close is grouped by host after reverse policy release. Cold changes exit old runtime before initializing the replacement. No automatic restart/replay; explicit reload then resume.
- Frame pool has three slots per camera. Unique pool/frame identities, immutable leased frames, read-only mappings, bounded allocation and explicit unlink. Exhaustion drops incoming frames. Resize replaces segments only when old leases permit.
- Health/runnable is distinct from outstanding reader state. All infer/observe/cancel/error paths require explicit images_released or matching runtime-tree exit proof. Post-queue submit failures remain asynchronous. View escape is a whole-host fatal error. Disconnection alone never frees frames.
- Hard timeout/crash kills the whole runtime tree, invalidates all host sessions and clears policy actions without stealing MCP ownership. Unconfirmed cleanup freezes frames until confirmed cleanup or complete stack teardown.

## Tasks

1. Shared image transport: failing real mapping/lease/bounds tests, replace file pool, run focused suite.
2. Host and RPC: tests for multi-policy same process, distinct helper packages, host preflight, requests and fatal/close handling; implement supervisor/runtime/client with lifecycle proofs.
3. RC integration: schema2, host preflight, grouped chain lifecycle, safe infer/observe leases; adapt existing regression fixtures and tests.
4. Docker/ROS: separate images/locked requirements, compile supplied messages + simulator, Compose IPC/UDS/overlays, instructions and startup helpers.
5. Verification: complete regression suite, actual Python3.10->3.14 containers, same-host/cross-host chain and fault scenarios, ROS simulator loop, fresh independent review, local Git commits.

## Acceptance

- Both image versions/imports and pip check pass; no host dependencies needed for normal deployment.
- Cross-container three-camera pixel equality and readonly views, no payload in RPC; multi-reader pinning/exhaustion/resize/ENOSPC/cleanup.
- A/B/A same runtime PID on shared host, separate host identities on split hosts; context and same-named helper isolation.
- Hanging initialize/infer/release, crashes, escaped views, lost ACK/disconnect after enqueue, stale results and Compose stop all preserve reader proofs and manual recovery.
- TUI/headless/MCP/config save/hot-cold changes/pause/snapshot/native chain regressions pass; Python3.14-only syntax fixture preflights remotely.
- Actual rclpy simulator loop passes separately from mock; never claim hardware verification without hardware.

## Evidence and ledger

Planning probe: Linux ARM64 Python3.14.9 + posix-ipc1.3.2 + NumPy2.5.4 compiled and passed readonly shared-memory mapping. Full implementation evidence lives in docs/verification.md; unique active ledger is .codex/task-state.md.
