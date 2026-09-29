@{
    # Lab topology. Update here, not in the scripts.

    VCenter = @{
        Fqdn = 'vcsa.lab.knowledgeondemand.net'
        Ip   = '172.16.10.129'
        # vCSA is a database appliance. It is shut down LAST and, by default,
        # is never hard-killed -- see StopGraceSeconds.VCenter below.
        VmName = 'vcsa'
    }

    # ESX hosts.
    #
    # There is no BMC. Wake-on-LAN gives remote power-ON only -- it cannot power
    # off, reset, or give you a console.
    #
    # WolMac must be the ONBOARD Realtek RTL8125 2.5GbE port, NOT a vmnic.
    # The cabled Intel 82599/X520 10GbE NICs do not support WoL at all (Intel:
    # "most Intel 10GbE adapters do not support WoL on any port"), and the 82599
    # spec additionally filters broadcast magic packets via the manageability
    # path. The onboard port needs no ESXi driver for this -- WoL is handled by
    # NIC firmware and BIOS in S5 -- but it DOES need to be cabled, and BIOS
    # must have WoL/PME enabled and ErP / Deep Sleep DISABLED (ErP cuts standby
    # power to the NIC, which silently defeats WoL).
    #
    # Leave WolMac empty until the port is cabled and the MAC is known; the
    # scripts then simply skip the wake attempt and wait for a manual power-on.
    Hosts = @(
        @{ Name = 'hyp01.lab.knowledgeondemand.net'; Ip = '172.16.10.145'; Short = 'hyp01'; WolMac = '' }
        @{ Name = 'hyp02.lab.knowledgeondemand.net'; Ip = '172.16.10.143'; Short = 'hyp02'; WolMac = '' }
        @{ Name = 'hyp03.lab.knowledgeondemand.net'; Ip = '172.16.10.141'; Short = 'hyp03'; WolMac = '' }
    )

    # Magic packets are L2 and do not route. They must be emitted onto the
    # segment the onboard NICs sit on. Set this to the directed broadcast for
    # that subnet -- e.g. '172.16.10.255' if the RTL8125 ports are on VLAN10,
    # or the appropriate broadcast for whichever VLAN you cable them to.
    # Note the lab's own switch must not drop directed broadcasts.
    WolBroadcast = '172.16.10.255'
    WolPorts     = @(9, 7)

    Cluster    = 'lab01-cluster-001'
    Datacenter = 'lab01-datacenter'

    # Management appliances, with the endpoint used to prove each is genuinely
    # serving. Power-on advances only when the gate passes -- never on power
    # state or an open port, both of which lie (rhttpproxy answers 443 long
    # before hostd is up).
    Appliances = @{
        SddcManager = @{ VmName = 'sddc-manager'; Ip = '172.16.10.133' }
        Nsx         = @{ VmName = 'nsxa';         Ip = '172.16.10.132' }
        Operations  = @{ VmName = 'ops';          Ip = '172.16.10.122' }
        License     = @{ VmName = 'licsrv';       Ip = '172.16.10.134' }
        OpsCollector= @{ VmName = 'opscollector'; Ip = '172.16.10.123' }
    }

    # VCF Supervisor Platform nodes.
    #
    # Control plane is identified by vCPU count (4) rather than by name, because
    # the supervisor destroys and recreates workers on its own -- node names are
    # NOT stable. Names below are a fallback for when vCenter is unavailable.
    Vsp = @{
        ControlPlaneVcpu = 4
        WorkerVcpu       = 10
        NamePrefix       = 'platform-'
        Folder           = 'vcf-management-services'
        # Observed 2026-09-27. Treat as a hint; discovery by vCPU wins.
        KnownControlPlane = @('platform-dlq9c','platform-rptxg','platform-62t8n')
        KnownWorkers      = @('platform-pfnmx','platform-c5t7j','platform-8n62z')
    }

    # ---------------------------------------------------------------- scope --
    # These scripts manage VCF components ONLY. Anything else on the cluster --
    # developer VMs, containers, appliances, one-off workloads -- is never
    # powered on or off, and never migrated. The managed set is:
    #
    #   * VCenter.VmName
    #   * every Appliances.*.VmName
    #   * VSP nodes matching Vsp.NamePrefix inside Vsp.Folder
    #
    # Anything outside that set is reported as "left alone" and otherwise
    # ignored. There is deliberately no flag to widen it: if you want a
    # non-VCF VM stopped, stop it yourself, so the decision is visible.
    #
    # Consequence worth knowing: a host cannot enter maintenance mode while any
    # VM runs on it. So Stop-VCFLab.ps1 -IncludeHosts will REFUSE if non-VCF
    # VMs are still running, and name them, rather than touch them.

    # Never power on, even though it lives in the VCF folders.
    NeverStart = @('vcf-services-runtime-template-*')

    # Per-component grace before a hard stop is considered.
    StopGraceSeconds = @{
        OpsCollector = 240
        License      = 180
        VspWorker    = 300
        VspControl   = 300
        Operations   = 300
        Nsx          = 420
        SddcManager  = 300
        # vCenter gets no hard stop by default. A vCSA hard-kill risks its
        # database. 0 = wait indefinitely; -Force overrides with this ceiling.
        VCenter      = 0
        VCenterForce = 900
    }

    GateTimeoutSeconds = @{
        HostApi       = 900
        VsanFormation = 900
        VCenterApi    = 1800
        SddcManager   = 1800
        Nsx           = 1800
        Operations    = 1800
        VspControl    = 900
        VspWorker     = 900
        Appliance     = 600
        HostShutdown  = 600
    }

    # Where credentials.env lives. A .psd1 must contain static data only -- no
    # variable expansion -- so this is resolved at load time by
    # Resolve-VCFLabCredentialPath, which tries, in order:
    #   1. this value, if it is an absolute path
    #   2. this value relative to the script directory
    #   3. this value relative to the user profile
    #   4. credentials.env next to the scripts
    #   5. %USERPROFILE%\.vcflab\credentials.env
    # So a credentials.env dropped beside these scripts just works, and an
    # absolute path here is honoured as written.
    CredentialFileRelative = '.vcflab\credentials.env'
}
