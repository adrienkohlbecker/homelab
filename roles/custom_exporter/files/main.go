package main

import (
	"fmt"
	"net/http"
	"os"
	"regexp"
	"strings"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	log "github.com/sirupsen/logrus"

	"os/exec"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

const DEFAULT_ADDRESS = ":9392"

func init() {
	getDriveActiveStatusInit()
}

func main() {
  r := prometheus.NewRegistry()
	r.MustRegister(driveActiveStatus)

  handler := promhttp.HandlerFor(r, promhttp.HandlerOpts{})
	http.Handle("/metrics", handler)
	gatherMetrics()
	go func() {
		for {
			gatherMetrics()
			time.Sleep(5*time.Second)
		}
	}()

	addr, ok := os.LookupEnv("LISTEN_ADDRESS")
	if !ok {
		addr = DEFAULT_ADDRESS
	}

	log.Info(fmt.Sprintf("Beginning to serve on address `%s`", addr))
	log.Fatal(http.ListenAndServe(addr, nil))
}

func gatherMetrics() {
	getDriveActiveStatus()
}

var driveActiveStatus = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "drive_active_status",
		Help: "Is set to 1 when the drive is not in standby",
	},
	[]string{
		// device id (from /dev/disk/by-id)
		"device",
		// state (`unknown`, `active/idle`, `standby`, `sleeping`)
		"state",
	},
)
var driveHdparmRegex = regexp.MustCompile("/dev/disk/by-id/([^/ ]+):\n drive state is:  ([\\w\\/]+)\n")
var driveHdparmStates = []string{"unknown", "active/idle", "standby", "sleeping"}
var driveHdparmDevices = []string{}

func getDriveActiveStatusInit() {
	value, ok := os.LookupEnv("DRIVE_HDPARM_DEVICES")
	if ok && value != "" {
		driveHdparmDevices = strings.Split(value,",")
	}

}

func getDriveActiveStatus() {
	if len(driveHdparmDevices) == 0 {
		return
	}

	// cmd := exec.Command("/bin/sh", "-c", "cat test.txt")
	cmd := exec.Command("/bin/sh", "-c", fmt.Sprintf("hdparm -C /dev/disk/by-id/%s", strings.Join(driveHdparmDevices, " /dev/disk/by-id/")))

	out, err := cmd.CombinedOutput()
	if err != nil {
		log.Fatalf("unable to execute hdparm: %s\n", err)
	}
	// Output:
	//
	// /dev/disk/by-id/ata-WDC_WD101EFBX-68B0AN0_VCJ3MYHP:
	// drive state is:  active/idle
	//
	// /dev/disk/by-id/ata-WDC_WD101EFBX-68B0AN0_VCJ3V79P:
	// drive state is:  active/idle

	matches := driveHdparmRegex.FindAllStringSubmatch(string(out), -1)
	if matches == nil {
		log.Fatal("unable to match output, result is nil\n", err)
	}

	data := map[string]string{}

	for _, m := range matches {
		device := m[1]
		state := m[2]

		data[device] = state
	}

	for _, d := range driveHdparmDevices {
		for _, s := range driveHdparmStates {
			var value float64
			if (data[d] == s) {
				value = 1
			}
			driveActiveStatus.With(prometheus.Labels{"device": d, "state": s}).Set(value)
		}
	}

}
