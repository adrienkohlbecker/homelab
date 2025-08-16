package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"path"
	"regexp"
	"strconv"
	"strings"

	"golang.org/x/exp/slices"

	"github.com/prometheus/client_golang/prometheus/promhttp"

	"os/exec"
	"time"

	"github.com/prometheus/client_golang/prometheus"
)

const DEFAULT_ADDRESS = ":19392"

var stderr *log.Logger
var stdout *log.Logger

func init() {
	stdout = log.New(os.Stdout, "", 0)
	stderr = log.New(os.Stderr, "", 0)
	getDriveActiveStatusInit()
	exporterUpInit()
	getNetdataContextStatusInit()
	getNetdataCollectorStatusInit()
}

func main() {
  r := prometheus.NewRegistry()
	r.MustRegister(exporterUp)
	r.MustRegister(exporterErrors)
	r.MustRegister(driveActiveGauge)
	r.MustRegister(driveStandbyGauge)
	r.MustRegister(driveSleepingGauge)
	r.MustRegister(driveUnknownGauge)
	r.MustRegister(cronLastSuccessTimestamp)
	r.MustRegister(cronNextRunTimestamp)
	r.MustRegister(netdataCollectorUp)

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

	stdout.Printf("Beginning to serve on address `%s`\n", addr)
	stderr.Fatal(http.ListenAndServe(addr, nil))
}

func gatherMetrics() {
	var err error
	err = getDriveActiveStatus()
	if err != nil {
		stderr.Printf("error during getDriveActiveStatus: %s\n", err)
		exporterErrors.Inc()
	}
	err = getCronLastSuccessTimestamp()
	if err != nil {
		stderr.Printf("error during getCronLastSuccessTimestamp: %s\n", err)
		exporterErrors.Inc()
	}
	err = getNetdataContextStatus()
	if err != nil {
		stderr.Printf("error during getNetdataContextStatus: %s\n", err)
		exporterErrors.Inc()
	}
	err = getNetdataCollectorStatus()
	if err != nil {
		stderr.Printf("error during getNetdataCollectorStatus: %s\n", err)
		exporterErrors.Inc()
	}
}

//  ██████╗██╗   ██╗███████╗████████╗ ██████╗ ███╗   ███╗    ███████╗██╗  ██╗██████╗  ██████╗ ██████╗ ████████╗███████╗██████╗
// ██╔════╝██║   ██║██╔════╝╚══██╔══╝██╔═══██╗████╗ ████║    ██╔════╝╚██╗██╔╝██╔══██╗██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝██╔══██╗
// ██║     ██║   ██║███████╗   ██║   ██║   ██║██╔████╔██║    █████╗   ╚███╔╝ ██████╔╝██║   ██║██████╔╝   ██║   █████╗  ██████╔╝
// ██║     ██║   ██║╚════██║   ██║   ██║   ██║██║╚██╔╝██║    ██╔══╝   ██╔██╗ ██╔═══╝ ██║   ██║██╔══██╗   ██║   ██╔══╝  ██╔══██╗
// ╚██████╗╚██████╔╝███████║   ██║   ╚██████╔╝██║ ╚═╝ ██║    ███████╗██╔╝ ██╗██║     ╚██████╔╝██║  ██║   ██║   ███████╗██║  ██║
//  ╚═════╝ ╚═════╝ ╚══════╝   ╚═╝    ╚═════╝ ╚═╝     ╚═╝    ╚══════╝╚═╝  ╚═╝╚═╝      ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝

var exporterUp = prometheus.NewGauge(
	prometheus.GaugeOpts{
		Name: "exporter_up",
		Help: "Always reports 1 when the service is up",
	},
)
var exporterErrors = prometheus.NewCounter(
	prometheus.CounterOpts{
		Name: "exporter_errors",
		Help: "Count the number of errors",
	},
)

func exporterUpInit() {
	exporterUp.Set(1)
}

func getJSON(url string, result interface{}) error {
	resp, err := http.Get(url)
	if err != nil {
			return fmt.Errorf("cannot fetch URL %q: %v", url, err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
			return fmt.Errorf("unexpected http GET status: %s", resp.Status)
	}
	// We could check the resulting content type
	// here if desired.
	err = json.NewDecoder(resp.Body).Decode(result)
	if err != nil {
			return fmt.Errorf("cannot decode JSON: %v", err)
	}
	return nil
}

// ██████╗ ██████╗ ██╗██╗   ██╗███████╗     █████╗  ██████╗████████╗██╗██╗   ██╗███████╗    ███████╗████████╗ █████╗ ████████╗██╗   ██╗███████╗
// ██╔══██╗██╔══██╗██║██║   ██║██╔════╝    ██╔══██╗██╔════╝╚══██╔══╝██║██║   ██║██╔════╝    ██╔════╝╚══██╔══╝██╔══██╗╚══██╔══╝██║   ██║██╔════╝
// ██║  ██║██████╔╝██║██║   ██║█████╗      ███████║██║        ██║   ██║██║   ██║█████╗      ███████╗   ██║   ███████║   ██║   ██║   ██║███████╗
// ██║  ██║██╔══██╗██║╚██╗ ██╔╝██╔══╝      ██╔══██║██║        ██║   ██║╚██╗ ██╔╝██╔══╝      ╚════██║   ██║   ██╔══██║   ██║   ██║   ██║╚════██║
// ██████╔╝██║  ██║██║ ╚████╔╝ ███████╗    ██║  ██║╚██████╗   ██║   ██║ ╚████╔╝ ███████╗    ███████║   ██║   ██║  ██║   ██║   ╚██████╔╝███████║
// ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝  ╚══════╝    ╚═╝  ╚═╝ ╚═════╝   ╚═╝   ╚═╝  ╚═══╝  ╚══════╝    ╚══════╝   ╚═╝   ╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚══════╝

var driveActiveGauge = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "hdparm_drive_active",
		Help: "Is set to 1 when the drive is active",
	},
	[]string{
		// device id (from /dev/disk/by-id)
		"device",
	},
)
var driveStandbyGauge = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "hdparm_drive_standby",
		Help: "Is set to 1 when the drive is in standby",
	},
	[]string{
		// device id (from /dev/disk/by-id)
		"device",
	},
)
var driveSleepingGauge = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "hdparm_drive_sleeping",
		Help: "Is set to 1 when the drive is sleeping",
	},
	[]string{
		// device id (from /dev/disk/by-id)
		"device",
	},
)
var driveUnknownGauge = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "hdparm_drive_unknown",
		Help: "Is set to 1 when the drive is in an unknown state",
	},
	[]string{
		// device id (from /dev/disk/by-id)
		"device",
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
		var active float64
		var sleeping float64
		var standby float64
		var unknown float64

		switch data[d] {
		case "active/idle":
			active = 1
		case "standby":
			standby = 1
		case "sleeping":
			sleeping = 1
		case "unknown":
			unknown = 1
		}

		driveActiveGauge.With(prometheus.Labels{"device": d}).Set(active)
		driveSleepingGauge.With(prometheus.Labels{"device": d}).Set(sleeping)
		driveStandbyGauge.With(prometheus.Labels{"device": d}).Set(standby)
		driveUnknownGauge.With(prometheus.Labels{"device": d}).Set(unknown)

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
var cronNextRunTimestamp = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "cron_next_run_timestamp",
		Help: "Set to the next time a cron job has to run (0 if it never ran once)",
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
		var nextValue float64

		if str != "" {

			//format: weekly 2024-05-18T13:33:06+00:00
			d := strings.Split(str, " ")
			if len(d) != 2 {
				loopErr = append(loopErr, fmt.Errorf("invalid format in job log for %s: %s", e.Name(), strconv.Quote(str)))
				continue
			}

			dataTime, err :=  time.Parse(time.RFC3339, d[1])
			if err != nil {
				loopErr = append(loopErr, fmt.Errorf("unable to parse date for %s: %s", e.Name(), err))
				continue
			}

			retry := 2 // allow for one failure without alert
			var nextDataTime time.Time
			switch d[0] {
			case "hourly":
				nextDataTime = dataTime.Add(time.Hour * time.Duration(retry))
			case "daily":
				nextDataTime = dataTime.AddDate(0,0,retry*1)
			case "weekly":
				nextDataTime = dataTime.AddDate(0,0,retry*7)
			case "monthly":
				nextDataTime = dataTime.AddDate(0,retry*1,0)
			default:
				loopErr = append(loopErr, fmt.Errorf("invalid frequency value for %s: %s", e.Name(), strconv.Quote(d[0])))
				continue
			}

			value = float64(dataTime.Unix())
			nextValue = float64(nextDataTime.Unix())

		}

		cronLastSuccessTimestamp.With(prometheus.Labels{"job": e.Name()}).Set(value)
		cronNextRunTimestamp.With(prometheus.Labels{"job": e.Name()}).Set(nextValue)

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

// ███╗   ██╗███████╗████████╗██████╗  █████╗ ████████╗ █████╗      ██████╗ ██████╗ ██╗     ██╗     ███████╗ ██████╗████████╗ ██████╗ ██████╗     ███████╗████████╗ █████╗ ████████╗██╗   ██╗███████╗
// ████╗  ██║██╔════╝╚══██╔══╝██╔══██╗██╔══██╗╚══██╔══╝██╔══██╗    ██╔════╝██╔═══██╗██║     ██║     ██╔════╝██╔════╝╚══██╔══╝██╔═══██╗██╔══██╗    ██╔════╝╚══██╔══╝██╔══██╗╚══██╔══╝██║   ██║██╔════╝
// ██╔██╗ ██║█████╗     ██║   ██║  ██║███████║   ██║   ███████║    ██║     ██║   ██║██║     ██║     █████╗  ██║        ██║   ██║   ██║██████╔╝    ███████╗   ██║   ███████║   ██║   ██║   ██║███████╗
// ██║╚██╗██║██╔══╝     ██║   ██║  ██║██╔══██║   ██║   ██╔══██║    ██║     ██║   ██║██║     ██║     ██╔══╝  ██║        ██║   ██║   ██║██╔══██╗    ╚════██║   ██║   ██╔══██║   ██║   ██║   ██║╚════██║
// ██║ ╚████║███████╗   ██║   ██████╔╝██║  ██║   ██║   ██║  ██║    ╚██████╗╚██████╔╝███████╗███████╗███████╗╚██████╗   ██║   ╚██████╔╝██║  ██║    ███████║   ██║   ██║  ██║   ██║   ╚██████╔╝███████║
// ╚═╝  ╚═══╝╚══════╝   ╚═╝   ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝     ╚═════╝ ╚═════╝ ╚══════╝╚══════╝╚══════╝ ╚═════╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝    ╚══════╝   ╚═╝   ╚═╝  ╚═╝   ╚═╝    ╚═════╝ ╚══════╝

var netdataCollectorUp = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "netdata_collector_up",
		Help: "Always reports 1 when the collector is up",
	},
	[]string{
		// collector identifier
		"collector",
	},
)

type NetdataContext struct {
	Family string
	Priority int
	FirstEntry int
	LastEntry int
	Live bool
}

type NetdataContextsResponse struct {
	Contexts map[string]NetdataContext
}

var netdataContexts map[string]string

func getNetdataContextStatusInit() {
	netdataContexts = make(map[string]string)

	value, ok := os.LookupEnv("NETDATA_CONTEXTS")
	if ok && value != "" {
		for _, v := range strings.Split(value,",") {
			vv := strings.Split(v, ":")
			if len(vv) != 2 {
				log.Fatalf("Invalid format for NETDATA_CONTEXTS env var, expected comma separated list of values separated by colon, got: %s", v)
			}

			netdataContexts[vv[1]] = vv[0]
		}
	}
}

func getNetdataContextStatus() error {

	if len(netdataContexts) == 0 {
		return nil
	}

	var resp NetdataContextsResponse
  err := getJSON("http://localhost:19999/api/v2/contexts", &resp)
	if (err != nil) {
		return err
	}

	metrics := map[string]float64{}

	for id, c := range resp.Contexts {
		collector, ok := netdataContexts[id]
		if !ok {
			continue
		}

		if c.Live {
			metrics[collector] = 1
		}
	}

	for _, collector := range netdataContexts {
		value, ok := metrics[collector]
		if !ok {
			value = 0
		}

		netdataCollectorUp.With(prometheus.Labels{"collector": collector}).Set(value)
	}

	return nil

}

type NetdataCollector struct {
	Type string
	Status string
	Sync bool
	UserDisabled bool
	RestartRequired bool
	PluginRejected bool
}

type NetdataConfigResponse struct {
	Tree map[string]map[string]NetdataCollector
}

var netdataCollectors []string

func getNetdataCollectorStatusInit() {
	value, ok := os.LookupEnv("NETDATA_COLLECTORS")
	if ok && value != "" {
		netdataCollectors = strings.Split(value,",")
	}
}

func getNetdataCollectorStatus() error {

	if len(netdataCollectors) == 0 {
		return nil
	}

	var resp NetdataConfigResponse
  err := getJSON("http://localhost:19999/api/v1/config", &resp)
	if (err != nil) {
		return err
	}

	metrics := map[string]float64{}

	for id, c := range resp.Tree["/collectors/jobs"] {
		if !slices.Contains(netdataCollectors, id) {
			continue
		}

		if !c.UserDisabled && !c.PluginRejected && !c.RestartRequired && c.Status == "running" {
			metrics[id] = 1
		}
	}

	for _, id := range netdataCollectors {
		if id == "" {
			continue
		}

		value, ok := metrics[id]
		if !ok {
			value = 0
		}

		netdataCollectorUp.With(prometheus.Labels{"collector": id}).Set(value)
	}

	return nil

}
