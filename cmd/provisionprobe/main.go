// provisionprobe receives video using a current ticket exported from Provision Home.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/url"
	"os"
	ossignal "os/signal"
	"strings"
	"sync/atomic"
	"time"

	"vico_home/native/internal/domain"
	a4xsignal "vico_home/native/internal/signal"
	"vico_home/native/internal/viewer"
	"vico_home/native/internal/webrtc"
)

type countingWriter struct {
	output io.Writer
	bytes  atomic.Int64
}

func (w *countingWriter) Write(data []byte) (int, error) {
	n, err := w.output.Write(data)
	w.bytes.Add(int64(n))
	return n, err
}

func run() error {
	ticketPath := flag.String("ticket", "", "local JSON session ticket exported from the app")
	serial := flag.String("serial", "", "serial number of the camera to view")
	targetIP := flag.String("target-ip", "", "require this camera address in remote ICE before requesting video")
	outputPath := flag.String("out", "", "local H264/H265 sample, '-' for a binary stdout stream, or empty to discard")
	duration := flag.Duration("duration", 15*time.Second, "1s to 60s for a probe, or 0 to stream until interrupted")
	resolution := flag.String("resolution", "1280x720", "requested stream resolution; the device may select a lower resolution")
	videoSize := flag.String("size", "medium", "requested app stream size")
	flag.Parse()
	if *ticketPath == "" || *serial == "" || *duration < 0 || (*duration > 0 && *duration < time.Second) || *duration > time.Minute {
		return fmt.Errorf("provide -ticket and -serial, with -duration 0 or between 1s and 60s")
	}
	if net.ParseIP(*targetIP) == nil {
		return fmt.Errorf("provide a valid -target-ip to verify the camera before receiving video")
	}
	raw, err := os.ReadFile(*ticketPath)
	if err != nil {
		return fmt.Errorf("read ticket: %w", err)
	}
	var wrapper struct {
		Data json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(raw, &wrapper); err != nil {
		return fmt.Errorf("decode ticket envelope: %w", err)
	}
	if len(wrapper.Data) != 0 {
		raw = wrapper.Data
	}
	var ticket domain.Ticket
	if err := json.Unmarshal(raw, &ticket); err != nil {
		return fmt.Errorf("decode ticket: %w", err)
	}
	endpoint, err := url.Parse(ticket.SignalServer)
	if err != nil || endpoint.Scheme != "wss" || !strings.HasSuffix(strings.ToLower(endpoint.Hostname()), ".smartvideogo.com") {
		return fmt.Errorf("ticket must use the app's TLS smartvideogo.com signaling endpoint")
	}
	if ticket.ExpirationTime <= time.Now().UnixMilli() || ticket.AccessToken == "" || ticket.ID == "" {
		return fmt.Errorf("ticket is expired or incomplete; refresh it from the phone")
	}
	if ticket.SignalPingInterval <= 0 {
		ticket.SignalPingInterval = 20
	}
	var output io.Writer = io.Discard
	if *outputPath == "-" {
		output = os.Stdout
	} else if *outputPath != "" {
		file, err := os.OpenFile(*outputPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			return fmt.Errorf("create sample without overwriting an existing file: %w", err)
		}
		defer file.Close()
		output = file
	}
	writer := &countingWriter{output: output}
	peer, err := webrtc.NewPeer(ticket.ICEServers, *serial)
	if err != nil {
		return err
	}
	defer peer.Close()
	peer.RequireTargetIP(*targetIP)
	peer.SetStreamQuality(*resolution, *videoSize)
	if err := peer.AddTransceivers(); err != nil {
		return err
	}
	peer.SetOnTrack(writer)
	ctx, cancel := ossignal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	if *duration > 0 {
		var timeoutCancel context.CancelFunc
		ctx, timeoutCancel = context.WithTimeout(ctx, *duration)
		defer timeoutCancel()
	}
	controller := viewer.New(peer, cancel)
	signaler := a4xsignal.NewClient(&ticket, *serial, controller)
	defer signaler.Close()
	controller.SetSignaler(signaler)
	peer.SetOnICECandidate(signaler.SendICECandidate)
	if err := signaler.Connect(); err != nil {
		return err
	}
	<-ctx.Done()
	signaler.Close()
	peer.Close()
	count := writer.bytes.Load()
	fmt.Fprintf(os.Stderr, "Received encoded video bytes: %d\n", count)
	if count == 0 {
		return fmt.Errorf("signaling completed without receiving video within the probe window")
	}
	return nil
}

func main() {
	if err := run(); err != nil {
		log.Print(err)
		os.Exit(1)
	}
}
