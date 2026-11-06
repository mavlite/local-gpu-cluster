# 2026-10-28 19:59:22 by RouterOS 7.20
# software id = IBEV-J3RM
#
# model = CRS309-1G-8S+
# serial number = HG509PPZTB3
/interface bridge add admin-mac=D4:01:C3:32:4C:7D auto-mac=no comment=defconf name=bridge vlan-filtering=yes
/interface ethernet set [ find default-name=ether1 ] l2mtu=9200 mtu=9200
/interface ethernet set [ find default-name=sfp-sfpplus1 ] auto-negotiation=no l2mtu=9200 mtu=9200
/interface ethernet set [ find default-name=sfp-sfpplus2 ] auto-negotiation=no l2mtu=9200 mtu=9200
/interface ethernet set [ find default-name=sfp-sfpplus3 ] auto-negotiation=no l2mtu=9200 mtu=9200
/interface ethernet set [ find default-name=sfp-sfpplus4 ] auto-negotiation=no l2mtu=9200 mtu=9200
/interface ethernet set [ find default-name=sfp-sfpplus5 ] auto-negotiation=no l2mtu=9200 mtu=9200
/interface ethernet set [ find default-name=sfp-sfpplus6 ] auto-negotiation=no l2mtu=9200 mtu=9200
/interface ethernet set [ find default-name=sfp-sfpplus7 ] auto-negotiation=no l2mtu=9200 mtu=9200
/interface ethernet set [ find default-name=sfp-sfpplus8 ] auto-negotiation=no l2mtu=9200 mtu=9200
/interface vlan add interface=bridge name=DevVM1-VLAN70 vlan-id=70
/interface vlan add interface=bridge name=DevVM2-VLAN71 vlan-id=71
/interface vlan add interface=bridge name=DevVM3-VLAN72 vlan-id=72
/interface vlan add interface=bridge mtu=9000 name=Mgmt-VLAN10 vlan-id=10
/interface vlan add interface=bridge name=Provision-VLAN99 vlan-id=99
/interface vlan add interface=bridge mtu=9000 name=T0-VLAN50 vlan-id=50
/interface vlan add interface=bridge mtu=9000 name=TEP-VLAN40 vlan-id=40
/interface vlan add interface=bridge mtu=9000 name=VM-Mgmt-VLAN60 vlan-id=60
/interface vlan add interface=bridge mtu=9000 name=vMotion-VLAN20 vlan-id=20
/interface vlan add interface=bridge mtu=9000 name=vSAN-VLAN30 vlan-id=30
/interface list add name=WAN
/interface list add name=LAN
/ip dhcp-server option sets add name=ipxe-http options=""
/ip pool add name=Provision ranges=172.16.99.100-172.16.99.150
/ip dhcp-server add address-pool=Provision interface=Provision-VLAN99 name=dhcp-provision
/port set 0 name=serial0
/interface bridge port add bridge=bridge comment=defconf interface=ether1
/interface bridge port add bridge=bridge comment=defconf interface=sfp-sfpplus1
/interface bridge port add bridge=bridge comment=defconf interface=sfp-sfpplus2 pvid=99
/interface bridge port add bridge=bridge comment=defconf interface=sfp-sfpplus3 pvid=99
/interface bridge port add bridge=bridge comment=defconf interface=sfp-sfpplus4 pvid=99
/interface bridge port add bridge=bridge comment=defconf interface=sfp-sfpplus5 pvid=99
/interface bridge port add bridge=bridge comment=defconf interface=sfp-sfpplus6 pvid=99
/interface bridge port add bridge=bridge comment=defconf interface=sfp-sfpplus7 pvid=99
/interface bridge port add bridge=bridge comment=defconf interface=sfp-sfpplus8
/interface bridge vlan add bridge=bridge comment=Mgmt tagged=all vlan-ids=10
/interface bridge vlan add bridge=bridge comment=vMotion tagged=all vlan-ids=20
/interface bridge vlan add bridge=bridge comment=vSAN tagged=all vlan-ids=30
/interface bridge vlan add bridge=bridge comment=TEP tagged=all vlan-ids=40
/interface bridge vlan add bridge=bridge comment=T0 tagged=all vlan-ids=50
/interface bridge vlan add bridge=bridge comment=VM-Mgmt tagged=all vlan-ids=60
/interface bridge vlan add bridge=bridge comment=Provisioning tagged=bridge untagged=sfp-sfpplus2,sfp-sfpplus3,sfp-sfpplus4,sfp-sfpplus5,sfp-sfpplus6,sfp-sfpplus7 vlan-ids=99
/interface bridge vlan add bridge=bridge tagged=all vlan-ids=70
/interface bridge vlan add bridge=bridge tagged=all vlan-ids=71
/interface bridge vlan add bridge=bridge tagged=all vlan-ids=72
/interface list member add interface=ether1 list=WAN
/interface list member add interface=sfp-sfpplus1 list=LAN
/interface list member add interface=sfp-sfpplus2 list=LAN
/interface list member add interface=sfp-sfpplus3 list=LAN
/interface list member add interface=sfp-sfpplus4 list=LAN
/interface list member add interface=sfp-sfpplus5 list=LAN
/interface list member add interface=sfp-sfpplus6 list=LAN
/interface list member add interface=sfp-sfpplus7 list=LAN
/interface list member add interface=sfp-sfpplus8 list=LAN
/ip address add address=192.168.6.11/24 comment=defconf interface=sfp-sfpplus1 network=192.168.6.0
/ip address add address=172.16.10.1/24 interface=Mgmt-VLAN10 network=172.16.10.0
/ip address add address=172.16.20.1/24 interface=vMotion-VLAN20 network=172.16.20.0
/ip address add address=172.16.30.1/24 interface=vSAN-VLAN30 network=172.16.30.0
/ip address add address=172.16.40.1/24 interface=TEP-VLAN40 network=172.16.40.0
/ip address add address=172.16.50.1/24 interface=T0-VLAN50 network=172.16.50.0
/ip address add address=172.16.60.1/24 interface=VM-Mgmt-VLAN60 network=172.16.60.0
/ip address add address=172.16.99.1/24 interface=Provision-VLAN99 network=172.16.99.0
/ip address add address=172.16.70.1/24 interface=DevVM1-VLAN70 network=172.16.70.0
/ip address add address=172.16.71.1/24 interface=DevVM2-VLAN71 network=172.16.71.0
/ip address add address=172.16.72.1/24 interface=DevVM3-VLAN72 network=172.16.72.0
/ip dhcp-server add address-pool=Mgmt interface=Mgmt-VLAN10 name=dhcp-mgmt
/ip dhcp-server add address-pool=vMotion interface=vMotion-VLAN20 name=dhcp-vmotion
/ip dhcp-server add address-pool=vSAN interface=vSAN-VLAN30 name=dhcp-vsan
/ip dhcp-server add address-pool=TEP interface=TEP-VLAN40 name=dhcp-tep
/ip dhcp-server add address-pool=TEP interface=T0-VLAN50 name=dhcp-t0
/ip dhcp-server add address-pool=VM-Mgmt interface=VM-Mgmt-VLAN60 name=VM-Mgmt
/ip dhcp-server lease add address=172.16.10.150 client-id=1:0:c:29:48:ec:b2 comment="dns01 - lab DNS/DC, MUST stay .150" mac-address=00:0C:29:48:EC:B2 server=dhcp-mgmt
/ip dhcp-server network add address=172.16.10.0/24 boot-file-name=snponly.efi dns-server=172.16.10.150 domain=knowledgeondemand.net gateway=172.16.10.1 netmask=24 next-server=172.16.10.1 ntp-server=172.16.10.1
/ip dhcp-server network add address=172.16.20.0/24 dns-server=172.16.10.150 domain=knowledgeondemand.net gateway=172.16.20.1 netmask=24 ntp-server=172.16.20.1
/ip dhcp-server network add address=172.16.30.0/24 dns-server=172.16.10.150 domain=knowledgeondemand.net gateway=172.16.30.1 netmask=24 ntp-server=172.16.30.1
/ip dhcp-server network add address=172.16.40.0/24 dns-server=172.16.10.150 domain=knowledgeondemand.net gateway=172.16.40.1 netmask=24 ntp-server=172.16.40.1
/ip dhcp-server network add address=172.16.50.0/24 dns-server=172.16.10.150 domain=knowledgeondemand.net gateway=172.16.50.1 netmask=24 ntp-server=172.16.50.1
/ip dhcp-server network add address=172.16.60.0/24 dns-server=172.16.10.150 domain=knowledgeondemand.net gateway=172.16.60.1 netmask=24 ntp-server=172.16.60.1
/ip dhcp-server network add address=172.16.99.0/24 boot-file-name=snponly.efi comment="PXE provisioning - untagged" dns-server=172.16.10.150 gateway=172.16.99.1 next-server=172.16.99.1
/ip dns set servers=172.16.10.150
/ip firewall filter add action=accept chain=forward comment=devvm-tester-filecopy dst-address=192.168.6.175 dst-port=21422 protocol=tcp src-address=172.16.70.0/24
/ip firewall filter add action=accept chain=forward comment=devvm-tester-filecopy dst-address=192.168.6.175 dst-port=21422 protocol=tcp src-address=172.16.71.0/24
/ip firewall filter add action=accept chain=forward comment=devvm-tester-filecopy dst-address=192.168.6.175 dst-port=21422 protocol=tcp src-address=172.16.72.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=10.0.0.0/8 src-address=172.16.70.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=172.16.0.0/12 src-address=172.16.70.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=192.168.0.0/16 src-address=172.16.70.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=100.64.0.0/10 src-address=172.16.70.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=169.254.0.0/16 src-address=172.16.70.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=10.0.0.0/8 src-address=172.16.71.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=172.16.0.0/12 src-address=172.16.71.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=192.168.0.0/16 src-address=172.16.71.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=100.64.0.0/10 src-address=172.16.71.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=169.254.0.0/16 src-address=172.16.71.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=10.0.0.0/8 src-address=172.16.72.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=172.16.0.0/12 src-address=172.16.72.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=192.168.0.0/16 src-address=172.16.72.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=100.64.0.0/10 src-address=172.16.72.0/24
/ip firewall filter add action=drop chain=forward comment=devvm-isolation dst-address=169.254.0.0/16 src-address=172.16.72.0/24
/ip firewall nat add action=masquerade chain=srcnat comment=devvm-isolation src-address=172.16.70.0/24
/ip firewall nat add action=masquerade chain=srcnat comment=devvm-isolation src-address=172.16.71.0/24
/ip firewall nat add action=masquerade chain=srcnat comment=devvm-isolation src-address=172.16.72.0/24
/ip pool add name=Mgmt next-pool=Mgmt ranges=172.16.10.200-172.16.10.230
/ip pool add name=vMotion next-pool=vMotion ranges=172.16.20.100-172.16.20.150
/ip pool add name=vSAN next-pool=vSAN ranges=172.16.30.100-172.16.30.150
/ip pool add name=TEP next-pool=TEP ranges=172.16.40.100-172.16.40.150
/ip pool add name=T0 next-pool=T0 ranges=172.16.50.100-172.16.50.150
/ip pool add name=VM-Mgmt next-pool=VM-Mgmt ranges=172.16.60.100-172.16.60.150
/ip tftp add ip-addresses=172.16.10.0/24,172.16.99.0/24 real-filename=flash/snponly.efi req-filename=snponly.efi
/system ntp client set enabled=yes
/system ntp server set enabled=yes use-local-clock=yes
/system ntp client servers add address=0.pool.ntp.org
/system ntp client servers add address=1.pool.ntp.org
/system routerboard settings set enter-setup-on=delete-key
