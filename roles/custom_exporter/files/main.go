package main

import (
	"fmt"
	"net/http"
	"os"
	"path"
	"regexp"
	"strconv"
	"strings"

	"golang.org/x/exp/slices"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	log "github.com/sirupsen/logrus"

	"os/exec"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

const DEFAULT_ADDRESS = ":19392"

func init() {
	getDriveActiveStatusInit()
}

func main() {
  r := prometheus.NewRegistry()
	r.MustRegister(driveActiveStatus)
	r.MustRegister(cronLastSuccessTimestamp)

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
	var err error
	err = getDriveActiveStatus()
	if err != nil {
		log.Errorf("error during getDriveActiveStatus: %s", err)
	}
	err = getCronLastSuccessTimestamp()
	if err != nil {
		log.Errorf("error during getCronLastSuccessTimestamp: %s", err)
	}
}

// ██████╗ ██████╗ ██╗██╗   ██╗███████╗     █████╗  ██████╗████████╗██╗██╗   ██╗███████╗    ███████╗████████╗ █████╗ ████████╗██╗   ██╗███████╗
// ██╔══██╗██╔══██╗██║██║   ██║██╔════╝    ██╔══██╗██╔════╝╚══██╔══╝██║██║   ██║██╔════╝    ██╔════╝╚══██╔══╝██╔══██╗╚══██╔══╝██║   ██║██╔════╝
// ██║  ██║██████╔╝██║██║   ██║█████╗      ███████║██║        ██║   ██║██║   ██║█████╗      ███████╗   ██║   ███████║   ██║   ██║   ██║███████╗
// ██║  ██║██╔══██╗██║╚██╗ ██╔╝██╔══╝      ██╔══██║██║        ██║   ██║╚██╗ ██╔╝██╔══╝      ╚════██║   ██║   ██╔══██║   ██║   ██║   ██║╚════██║
// ██████╔╝██║  ██║██║ ╚████╔╝ ███████╗    ██║  ██║╚██████╗   ██║   ██║ ╚████╔╝ ███████╗    ███████║   ██║   ██║  ██║   ██║   ╚██████╔╝███████║
// ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝  ╚══════╝    ╚═╝  ╚═╝ ╚═════╝   ╚═╝   ╚═╝  ╚═══╝  ╚══════╝    ╚══════╝   ╚═╝   ╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚══════╝

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

func getDriveActiveStatus() error {
	if len(driveHdparmDevices) == 0 {
		return nil
	}

	// cmd := exec.Command("/bin/sh", "-c", "cat test/test.txt")
	cmd := exec.Command("/bin/sh", "-c", fmt.Sprintf("hdparm -C /dev/disk/by-id/%s", strings.Join(driveHdparmDevices, " /dev/disk/by-id/")))

	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("unable to execute hdparm: %s", err)
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
		return fmt.Errorf("unable to match output, result is nil: %s", strconv.Quote(string(out)))
	}

	data := map[string]string{}

	var loopErr []error
	for _, m := range matches {
		device := m[1]
		state := m[2]

		if (!slices.Contains(driveHdparmDevices, device)) {
			loopErr = append(loopErr, fmt.Errorf("invalid device found: %s", device))
		}
		if (!slices.Contains(driveHdparmStates, state)) {
			loopErr = append(loopErr, fmt.Errorf("invalid state found: %s", state))
		}

		data[device] = state
	}

	if len(loopErr) > 0 {
		var errorMsg []string
		for _, e := range loopErr {
			errorMsg = append(errorMsg, e.Error())
		}
		return fmt.Errorf("unable to hdparm output: %s", strings.Join(errorMsg, ", "))
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

	return nil

}

//  ██████╗██████╗  ██████╗ ███╗   ██╗    ██╗      █████╗ ███████╗████████╗    ███████╗██╗   ██╗ ██████╗ ██████╗███████╗███████╗███████╗    ████████╗██╗███╗   ███╗███████╗███████╗████████╗ █████╗ ███╗   ███╗██████╗
// ██╔════╝██╔══██╗██╔═══██╗████╗  ██║    ██║     ██╔══██╗██╔════╝╚══██╔══╝    ██╔════╝██║   ██║██╔════╝██╔════╝██╔════╝██╔════╝██╔════╝    ╚══██╔══╝██║████╗ ████║██╔════╝██╔════╝╚══██╔══╝██╔══██╗████╗ ████║██╔══██╗
// ██║     ██████╔╝██║   ██║██╔██╗ ██║    ██║     ███████║███████╗   ██║       ███████╗██║   ██║██║     ██║     █████╗  ███████╗███████╗       ██║   ██║██╔████╔██║█████╗  ███████╗   ██║   ███████║██╔████╔██║██████╔╝
// ██║     ██╔══██╗██║   ██║██║╚██╗██║    ██║     ██╔══██║╚════██║   ██║       ╚════██║██║   ██║██║     ██║     ██╔══╝  ╚════██║╚════██║       ██║   ██║██║╚██╔╝██║██╔══╝  ╚════██║   ██║   ██╔══██║██║╚██╔╝██║██╔═══╝
// ╚██████╗██║  ██║╚██████╔╝██║ ╚████║    ███████╗██║  ██║███████║   ██║       ███████║╚██████╔╝╚██████╗╚██████╗███████╗███████║███████║       ██║   ██║██║ ╚═╝ ██║███████╗███████║   ██║   ██║  ██║██║ ╚═╝ ██║██║
//  ╚═════╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝    ╚══════╝╚═╝  ╚═╝╚══════╝   ╚═╝       ╚══════╝ ╚═════╝  ╚═════╝ ╚═════╝╚══════╝╚══════╝╚══════╝       ╚═╝   ╚═╝╚═╝     ╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚═╝     ╚═╝╚═╝


var cronLastSuccessTimestamp = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "cron_last_success_timestamp",
		Help: "Set to the last time a cron job succeeded (0 if it never did)",
	},
	[]string{
		// job identifier
		"job",
	},
)

func getCronLastSuccessTimestamp() error {

	// dir := "test/jobs"
	dir := "/var/log/jobs"

	entries, err := os.ReadDir(dir)
	if err != nil {
			return fmt.Errorf("unable to read jobs directory: %s", err)
	}

	var loopErr []error
	for _, e := range entries {

		p := path.Join(dir, e.Name())
		buf, err := os.ReadFile(p)
		if err != nil {
			loopErr = append(loopErr, fmt.Errorf("unable to read %s: %s", p, err))
			continue
		}

		str := strings.TrimSpace(string(buf))
		var value float64

		if str != "" {

			dataTime, err :=  time.Parse(time.RFC3339, str)
			if err != nil {
				loopErr = append(loopErr, fmt.Errorf("unable to parse date from %s: %s", p, err))
				continue
			}

			value = float64(dataTime.Unix())
		}

		cronLastSuccessTimestamp.With(prometheus.Labels{"job": e.Name()}).Set(value)

	}

	if len(loopErr) > 0 {
		var errorMsg []string
		for _, e := range loopErr {
			errorMsg = append(errorMsg, e.Error())
		}
		return fmt.Errorf("unable to process jobs log directory: %s", strings.Join(errorMsg, ", "))
	}

	return nil

}
