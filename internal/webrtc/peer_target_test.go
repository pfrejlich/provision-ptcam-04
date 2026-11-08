package webrtc

import "testing"

func TestCameraAddressGuard(t *testing.T) {
	cases := []struct {
		name      string
		candidate string
		matched   bool
	}{
		{"requested camera", "candidate:1 1 udp 123 192.0.2.10 55400 typ host", true},
		{"other camera", "candidate:1 1 udp 123 192.0.2.11 55400 typ host", false},
		{"relay does not identify camera", "candidate:2 1 udp 123 203.0.113.12 55400 typ relay", false},
		{"address substring does not match", "candidate:1 1 udp 123 192.0.2.100 55400 typ host", false},
		{"malformed message", "192.0.2.10", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			peer := &Peer{targetIP: "192.0.2.10"}
			peer.observeRemoteCandidate(tc.candidate)
			if got := peer.targetMatched.Load(); got != tc.matched {
				t.Fatalf("camera match = %v, want %v", got, tc.matched)
			}
		})
	}
}
