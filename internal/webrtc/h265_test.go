package webrtc

import (
	"bytes"
	"testing"
)

func TestH265FragmentReassemblyAcrossSequenceWrap(t *testing.T) {
	d := &H265Depacketizer{}
	if got := d.Depacketize(65535, []byte{98, 1, 0x93, 0xaa, 0xbb}); len(got) != 0 {
		t.Fatal("incomplete fragment produced a NAL unit")
	}
	got := d.Depacketize(0, []byte{98, 1, 0x53, 0xcc, 0xdd})
	want := []byte{38, 1, 0xaa, 0xbb, 0xcc, 0xdd}
	if len(got) != 1 || !bytes.Equal(got[0], want) {
		t.Fatalf("fragment reassembly = %x, want %x", got, want)
	}
}

func TestH265MissingFragmentDropsDamagedFrame(t *testing.T) {
	d := &H265Depacketizer{}
	d.Depacketize(10, []byte{98, 1, 0x93, 1, 2})
	if got := d.Depacketize(12, []byte{98, 1, 0x53, 3, 4}); len(got) != 0 {
		t.Fatal("a frame with a missing RTP fragment must be discarded")
	}
}
