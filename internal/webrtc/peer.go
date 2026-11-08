package webrtc

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"vico_home/native/internal/domain"

	"github.com/pion/interceptor"
	pion "github.com/pion/webrtc/v4"
)

// Peer wraps a Pion PeerConnection and DataChannel.
type Peer struct {
	pc            *pion.PeerConnection
	dc            *pion.DataChannel
	serialNumber  string
	remoteDescSet chan struct{}
	targetIP      string
	targetMatched atomic.Bool
	channelOpen   atomic.Bool
	startOnce     sync.Once
	resolution    string
	videoSize     string
}

// NewPeer creates a PeerConnection with minimal codec registration and a DataChannel.
func NewPeer(iceServers []domain.ICEServer, serialNumber string) (*Peer, error) {
	m := &pion.MediaEngine{}
	if err := m.RegisterCodec(pion.RTPCodecParameters{
		RTPCodecCapability: pion.RTPCodecCapability{
			MimeType: pion.MimeTypeH265, ClockRate: 90000,
		},
		PayloadType: 125,
	}, pion.RTPCodecTypeVideo); err != nil {
		return nil, fmt.Errorf("register H265: %w", err)
	}

	h264Codec := pion.RTPCodecParameters{
		RTPCodecCapability: pion.RTPCodecCapability{
			MimeType:    pion.MimeTypeH264,
			ClockRate:   90000,
			SDPFmtpLine: "level-asymmetry-allowed=1;packetization-mode=0;profile-level-id=64001f",
		},
		PayloadType: 121,
	}
	if err := m.RegisterCodec(h264Codec, pion.RTPCodecTypeVideo); err != nil {
		return nil, fmt.Errorf("register H264: %w", err)
	}

	pcmuCodec := pion.RTPCodecParameters{
		RTPCodecCapability: pion.RTPCodecCapability{
			MimeType:  pion.MimeTypePCMU,
			ClockRate: 8000,
			Channels:  1,
		},
		PayloadType: 0,
	}
	if err := m.RegisterCodec(pcmuCodec, pion.RTPCodecTypeAudio); err != nil {
		return nil, fmt.Errorf("register PCMU: %w", err)
	}

	i := &interceptor.Registry{}
	if err := pion.RegisterDefaultInterceptors(m, i); err != nil {
		return nil, fmt.Errorf("register default interceptors: %w", err)
	}

	api := pion.NewAPI(
		pion.WithMediaEngine(m),
		pion.WithInterceptorRegistry(i),
	)

	var servers []pion.ICEServer
	for _, s := range iceServers {
		servers = append(servers, pion.ICEServer{
			URLs:       []string{s.URL},
			Username:   s.Username,
			Credential: s.Credential,
		})
	}

	pc, err := api.NewPeerConnection(pion.Configuration{
		ICEServers:   servers,
		BundlePolicy: pion.BundlePolicyMaxBundle,
	})
	if err != nil {
		return nil, fmt.Errorf("create peer connection: %w", err)
	}

	dc, err := pc.CreateDataChannel(serialNumber, nil)
	if err != nil {
		pc.Close()
		return nil, fmt.Errorf("create data channel: %w", err)
	}

	p := &Peer{
		pc:            pc,
		dc:            dc,
		serialNumber:  serialNumber,
		remoteDescSet: make(chan struct{}),
		resolution:    "1280x720",
		videoSize:     "medium",
	}

	dc.OnOpen(func() {
		log.Printf("[webrtc] data channel opened")
		p.channelOpen.Store(true)
		p.tryStartLive()
	})
	dc.OnMessage(func(msg pion.DataChannelMessage) {
		log.Printf("[webrtc] data channel message: %d bytes", len(msg.Data))
		var fields map[string]json.RawMessage
		if json.Unmarshal(msg.Data, &fields) == nil {
			for _, key := range []string{"action", "code", "result", "status", "resolution", "size", "codec", "videoCodec"} {
				if value, ok := fields[key]; ok && len(value) < 100 {
					log.Printf("[webrtc] camera response %s=%s", key, value)
				}
			}
		}
	})
	dc.OnClose(func() {
		log.Printf("[webrtc] data channel closed")
	})

	pc.OnICEConnectionStateChange(func(state pion.ICEConnectionState) {
		log.Printf("[webrtc] ICE connection state: %s", state.String())
	})
	pc.OnConnectionStateChange(func(state pion.PeerConnectionState) {
		log.Printf("[webrtc] peer connection state: %s", state.String())
	})

	return p, nil
}

// AddTransceivers adds audio (sendrecv) and video (recvonly) transceivers.
func (p *Peer) AddTransceivers() error {
	_, err := p.pc.AddTransceiverFromKind(pion.RTPCodecTypeAudio, pion.RTPTransceiverInit{
		Direction: pion.RTPTransceiverDirectionSendrecv,
	})
	if err != nil {
		return fmt.Errorf("add audio transceiver: %w", err)
	}

	_, err = p.pc.AddTransceiverFromKind(pion.RTPCodecTypeVideo, pion.RTPTransceiverInit{
		Direction: pion.RTPTransceiverDirectionRecvonly,
	})
	if err != nil {
		return fmt.Errorf("add video transceiver: %w", err)
	}

	return nil
}

// SetOnTrack writes H264 or H265 video to videoOut and drains audio.
func (p *Peer) SetOnTrack(videoOut io.Writer) {
	p.pc.OnTrack(func(track *pion.TrackRemote, receiver *pion.RTPReceiver) {
		codec := track.Codec()
		log.Printf("[webrtc] got track: kind=%s codec=%s pt=%d", track.Kind(), codec.MimeType, codec.PayloadType)

		if track.Kind() == pion.RTPCodecTypeVideo {
			go p.readVideoTrack(track, videoOut)
		} else {
			go func() {
				buf := make([]byte, 1500)
				for {
					_, _, err := track.Read(buf)
					if err != nil {
						return
					}
				}
			}()
		}
	})
}

func (p *Peer) readVideoTrack(track *pion.TrackRemote, w io.Writer) {
	log.Printf("[webrtc] reading %s video track", track.Codec().MimeType)

	startCode := []byte{0x00, 0x00, 0x00, 0x01}
	var depack interface {
		Depacketize(uint16, []byte) [][]byte
	} = NewH264Depacketizer()
	if strings.EqualFold(track.Codec().MimeType, pion.MimeTypeH265) {
		depack = &H265Depacketizer{}
	}

	for {
		pkt, _, err := track.ReadRTP()
		if err != nil {
			log.Printf("[webrtc] video track read error: %v", err)
			return
		}

		nalus := depack.Depacketize(pkt.SequenceNumber, pkt.Payload)
		for _, nalu := range nalus {
			if p.targetIP != "" && !p.targetMatched.Load() {
				continue
			}
			if len(nalu) == 0 {
				continue
			}
			if _, err := w.Write(startCode); err != nil {
				log.Printf("[webrtc] video write start code error: %v", err)
				return
			}
			if _, err := w.Write(nalu); err != nil {
				log.Printf("[webrtc] video write nalu error: %v", err)
				return
			}
		}
	}
}

// SetOnICECandidate registers the callback for locally discovered ICE candidates.
func (p *Peer) SetOnICECandidate(send func(sdpMid string, sdpMLineIndex int, candidate string)) {
	p.pc.OnICECandidate(func(c *pion.ICECandidate) {
		if c == nil {
			log.Printf("[webrtc] ICE gathering complete")
			return
		}

		candidateStr := c.ToJSON().Candidate
		if isLoopback(candidateStr) {
			log.Printf("[webrtc] filtering loopback ICE candidate")
			return
		}

		sdpMid := ""
		if c.ToJSON().SDPMid != nil {
			sdpMid = *c.ToJSON().SDPMid
		}
		sdpMLineIndex := 0
		if c.ToJSON().SDPMLineIndex != nil {
			sdpMLineIndex = int(*c.ToJSON().SDPMLineIndex)
		}

		log.Printf("[webrtc] local ICE candidate: %s", candidateStr)
		send(sdpMid, sdpMLineIndex, candidateStr)
	})
}

// CreateOffer creates an SDP offer and sets it as the local description.
func (p *Peer) CreateOffer() (string, error) {
	offer, err := p.pc.CreateOffer(nil)
	if err != nil {
		return "", fmt.Errorf("create offer: %w", err)
	}

	if err := p.pc.SetLocalDescription(offer); err != nil {
		return "", fmt.Errorf("set local description: %w", err)
	}

	log.Printf("[webrtc] local SDP offer set")
	return offer.SDP, nil
}

// SetRemoteDescription sets the SDP answer and unblocks remote ICE candidate addition.
func (p *Peer) SetRemoteDescription(sdp domain.SDPPayload) error {
	answer := pion.SessionDescription{
		Type: pion.SDPTypeAnswer,
		SDP:  sdp.SDP,
	}

	if err := p.pc.SetRemoteDescription(answer); err != nil {
		return fmt.Errorf("set remote description: %w", err)
	}
	for _, line := range strings.Split(sdp.SDP, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "m=") || strings.HasPrefix(line, "a=rtpmap:") {
			log.Printf("[webrtc] negotiated %s", line)
		}
		if strings.HasPrefix(line, "a=candidate:") {
			p.observeRemoteCandidate(strings.TrimPrefix(line, "a="))
		}
	}

	log.Printf("[webrtc] remote SDP answer set")
	close(p.remoteDescSet)
	return nil
}

// AddRemoteICECandidate waits for the remote description to be set, then adds the candidate.
func (p *Peer) AddRemoteICECandidate(candidate domain.ICECandidatePayload) error {
	<-p.remoteDescSet

	sdpMLineIndex := uint16(candidate.SDPMLineIndex)
	init := pion.ICECandidateInit{
		Candidate:     candidate.Candidate,
		SDPMid:        &candidate.SDPMid,
		SDPMLineIndex: &sdpMLineIndex,
	}

	if err := p.pc.AddICECandidate(init); err != nil {
		return fmt.Errorf("add ice candidate: %w", err)
	}
	p.observeRemoteCandidate(candidate.Candidate)

	log.Printf("[webrtc] added remote ICE candidate")
	return nil
}

// RequireTargetIP prevents requesting or writing video until the camera advertises
// the expected address. A relay-only session cannot satisfy this identity check.
func (p *Peer) RequireTargetIP(ip string) {
	p.targetIP = ip
}

// SetStreamQuality sets the app protocol's requested quality before connecting.
func (p *Peer) SetStreamQuality(resolution, size string) {
	p.resolution, p.videoSize = resolution, size
}

func (p *Peer) observeRemoteCandidate(candidate string) {
	fields := strings.Fields(candidate)
	if p.targetIP == "" || len(fields) < 8 || !strings.HasPrefix(fields[0], "candidate:") {
		return
	}
	log.Printf("[webrtc] remote ICE address: %s", fields[4])
	if fields[4] == p.targetIP {
		p.targetMatched.Store(true)
		log.Printf("[webrtc] camera advertised the requested address %s", p.targetIP)
		p.tryStartLive()
	}
}

func (p *Peer) tryStartLive() {
	if p.channelOpen.Load() && (p.targetIP == "" || p.targetMatched.Load()) {
		p.startOnce.Do(p.sendStartLive)
	}
}

// startLiveCommand is the JSON command sent over the DataChannel to start live streaming.
type startLiveCommand struct {
	Action       string `json:"action"`
	RequestID    string `json:"requestID"`
	ConnectionID string `json:"connectionID"`
	TimeStamp    string `json:"timeStamp"`
	Size         string `json:"size"`
	Resolution   string `json:"resolution"`
}

func (p *Peer) sendStartLive() {
	ts := strconv.FormatInt(time.Now().UnixMilli(), 10)
	cmd := startLiveCommand{
		Action:       "startLive",
		RequestID:    ts,
		ConnectionID: "",
		TimeStamp:    ts,
		Size:         p.videoSize,
		Resolution:   p.resolution,
	}

	data, _ := json.Marshal(cmd)
	log.Printf("[webrtc] sending startLive: %s", string(data))
	if err := p.dc.SendText(string(data)); err != nil {
		log.Printf("[webrtc] sendStartLive error: %v", err)
	}
}

// Close shuts down the DataChannel and PeerConnection.
func (p *Peer) Close() {
	if p.dc != nil {
		p.dc.Close()
	}
	if p.pc != nil {
		p.pc.Close()
	}
}

func isLoopback(candidate string) bool {
	return strings.Contains(candidate, "127.0.0.1") || strings.Contains(candidate, "::1 ")
}
