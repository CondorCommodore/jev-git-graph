# Reviewed deploy-sync runtime compatibility

The Jev baseline in `coordinator.py` remains pinned to the Home Lab canonical tree at `c82cea2a96eec1d303743081710816673abffba0`: `scripts/deploy_sync.py` SHA-256 `bb54d9ede1a318951c109b8453c7ace28accc9f38867b3272c4e0891b67ec84d`.

The observed stable and compatibility release is Home Lab commit `dc91a90d0965ccf1aba3eba3dca71f8e8061e806`. Its `scripts/deploy_sync.py` SHA-256 is `6c702771a35b2fb3ab8a101a62d04973adbd03ec888d2eef8a498e895e2a22bd`. The only file delta from c82 to dc91 is commit `48e4c0572f5345cfb333f43441f772fd31b7b5fc`, which adds a fail-closed `HOSTS` membership check for unknown hosts before the existing lock paths.

Runtime hook verification accepts those two exact hashes only for `scripts/deploy_sync.py`. It leaves the original code-owned baseline intact. Every other hook remains exact. The verified file's actual SHA continues into root-labeled capability hook data and process-generation digests, so a mid-operation root/hash change remains detectable. Process and supervised-shell startup attestation expectations are unchanged.

## Reviewed merge-loop runtime compatibility

The unchanged Jev baselines also remain pinned for `scripts/merge-safe-prs-loop.sh` and `scripts/merge_train_parts/candidate_lifecycle.py`. Runtime verification additionally accepts shell SHA-256 `2ec5e69c594d813624161ccdffe4a92bd6ed184999f7469b3f8a219e67563086` and lifecycle SHA-256 `b798333fe2373716c80520ec37d9349e98e6dcc09147b808058addfab707d6f7`, respectively. These are path-specific exact variants; no other hook accepts them.

For the supervised merge-loop shell receipt, `source_sha256` and `loaded_code_sha256` must both equal the digest captured for that exact runtime root during hook validation. Attestation rereads the shell and fails if its bytes no longer match that captured digest. Thus an accepted variant is recorded as the root's actual digest in capability and process-generation data; capability equality and generation fencing remain strict. The supervisor, helper, wrappers, and all other startup receipt fields remain pinned to their original exact requirements.
