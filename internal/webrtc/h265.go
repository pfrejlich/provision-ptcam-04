package webrtc

import "github.com/pion/rtp/codecs"

// H265Depacketizer assembles RFC 7798 NAL units without DONL interleaving.
type H265Depacketizer struct {
	buffer  []byte
	next    uint16
	started bool
}

func (d *H265Depacketizer) Depacketize(sequence uint16, payload []byte) [][]byte {
	packet := &codecs.H265Packet{}
	if _, err := packet.Unmarshal(payload); err != nil {
		d.buffer, d.started = nil, false
		return nil
	}
	switch value := packet.Packet().(type) {
	case *codecs.H265SingleNALUnitPacket:
		return [][]byte{payload}
	case *codecs.H265AggregationPacket:
		units := [][]byte{value.FirstUnit().NalUnit()}
		for _, other := range value.OtherUnits() {
			units = append(units, other.NalUnit())
		}
		return units
	case *codecs.H265FragmentationUnitPacket:
		if value.FuHeader().S() {
			header := uint16(value.PayloadHeader()) & 0x81ff
			header |= uint16(value.FuHeader().FuType()) << 9
			d.buffer = []byte{byte(header >> 8), byte(header)}
			d.started = true
		} else if !d.started || sequence != d.next {
			d.buffer, d.started = nil, false
			return nil
		}
		d.next = sequence + 1
		d.buffer = append(d.buffer, value.Payload()...)
		if value.FuHeader().E() {
			unit := d.buffer
			d.buffer, d.started = nil, false
			return [][]byte{unit}
		}
	}
	return nil
}
