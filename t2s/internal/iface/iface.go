// Package iface finds this computer's network interfaces: the MAC address of the NIC a
// connection leaves by, which t2s reports with a result so a network admin can find the
// computer on a switch port or wireless access point.
package iface

import (
	"net"
	"strings"
)

// MACOf is the MAC address of the interface that holds localIP, the address a connection
// to server left from: the interface the system routed it out of. "" for one without a
// MAC (a tunnel, loopback, a VPN adapter).
func MACOf(localIP, server string) string {
	local := net.ParseIP(strings.Split(localIP, "%")[0])
	if local == nil {
		return ""
	}
	links, err := net.Interfaces()
	if err != nil {
		return ""
	}
	for _, link := range links {
		addrs, err := link.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			ip, _, err := net.ParseCIDR(a.String())
			if err != nil || !ip.Equal(local) {
				continue
			}
			mac := link.HardwareAddr
			if len(mac) != 6 || isZero(mac) {
				return ""
			}
			return strings.ToLower(mac.String())
		}
	}
	return ""
}

func isZero(mac net.HardwareAddr) bool {
	for _, b := range mac {
		if b != 0 {
			return false
		}
	}
	return true
}
