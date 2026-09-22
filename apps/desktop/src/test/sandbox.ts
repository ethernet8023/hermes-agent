import { currentSandboxOwner, type SandboxOwner } from '@/api/sandbox'
import { $activeGatewayProfile } from '@/store/profile'
import { publishSandboxStatus } from '@/store/sandbox'

/** Explicit non-MXC fixture for tests exercising rich model-output rendering. */
export function confirmNonMxcOwner(owner: SandboxOwner = currentSandboxOwner($activeGatewayProfile.get())): SandboxOwner {
  publishSandboxStatus({
    available: false,
    containers_started: 0,
    degraded: false,
    enabled: false,
    os_build: '',
    platform_supported: false,
    policy: { network: false, readonly_paths: [], readwrite_paths: [] },
    reason: null,
    shell_missing: false,
    shell_path: null,
    tier: null,
    warnings: [],
    wxc_exec_path: null
  }, owner)

  return owner
}
