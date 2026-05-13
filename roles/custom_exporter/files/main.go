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
	"github.com/prometheus/client_golang/prometheus/collectors"
)

const DEFAULT_ADDRESS = "127.0.0.1:19392"

var stderr *log.Logger
var stdout *log.Logger

func init() {
	stdout = log.New(os.Stdout, "", 0)
	stderr = log.New(os.Stderr, "", 0)
	getDriveActiveStatusInit()
	exporterUpInit()
	getNetdataCollectorStatusInit()
}

func main() {
	// Parse NETDATA_CONTEXTS here rather than in init() so an env-format error
	// surfaces as a normal Fatal log line with the loggers already wired up,
	// not a panic-style stderr dump from the Go runtime before main() runs.
	if err := getNetdataContextStatusInit(); err != nil {
		stderr.Fatal(err)
	}

	r := prometheus.NewRegistry()
	// Standard Go runtime + process collectors -- go_goroutines, process_resident_memory_bytes,
	// process_cpu_seconds_total, etc. Lets us spot a leaking or wedged exporter from outside.
	r.MustRegister(collectors.NewGoCollector())
	r.MustRegister(collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	r.MustRegister(exporterUp)
	r.MustRegister(exporterErrors)
	r.MustRegister(gatherLastIterationTimestamp)
	r.MustRegister(driveActiveGauge)
	r.MustRegister(driveStandbyGauge)
	r.MustRegister(driveSleepingGauge)
	r.MustRegister(driveUnknownGauge)
	r.MustRegister(cronLastSuccessTimestamp)
	r.MustRegister(cronNextRunTimestamp)
	r.MustRegister(netdataContextUp)
	r.MustRegister(netdataCollectorUp)

	handler := promhttp.HandlerFor(r, promhttp.HandlerOpts{})
	http.Handle("/metrics", handler)
	gatherMetrics()
	go func() {
		for {
			gatherMetrics()
			time.Sleep(5 * time.Second)
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
	gatherLastIterationTimestamp.Set(float64(time.Now().Unix()))
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

// Heartbeat for the gather goroutine. exporter_up only reflects whether the
// process is alive -- this gauge advances each loop iteration, so an alert
// on `$now - $this` catches the case where the goroutine wedges (e.g. on a
// blocked syscall) while the HTTP server keeps serving stale metrics.
var gatherLastIterationTimestamp = prometheus.NewGauge(
	prometheus.GaugeOpts{
		Name: "gather_last_iteration_timestamp",
		Help: "Unix time the most recent gatherMetrics() call returned",
	},
)

func exporterUpInit() {
	exporterUp.Set(1)
}

// Bounded client for the localhost netdata API. The scrape goroutine calls
// getJSON sequentially every 5s; without a timeout a wedged netdata stalls the
// entire loop while exporter_up keeps reporting 1.
var netdataClient = &http.Client{Timeout: 3 * time.Second}

func getJSON(url string, result interface{}) error {
	resp, err := netdataClient.Get(url)
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
		driveHdparmDevices = strings.Split(value, ",")
	}
}

func getDriveActiveStatus() error {
	if len(driveHdparmDevices) == 0 {
		return nil
	}

	args := []string{"-C"}
	for _, d := range driveHdparmDevices {
		args = append(args, "/dev/disk/by-id/"+d)
	}
	cmd := exec.Command("hdparm", args...)

	// hdparm continues past per-drive failures and emits valid sections for the
	// drives that did respond; parse stdout even on non-zero exit so a single
	// flaky drive doesn't pin the rest at stale gauge values. Drives missing
	// from the parsed output fall through to unknown=1, which feeds the
	// hdparm_drive_unknown alert.
	out, runErr := cmd.CombinedOutput()
	// Output:
	//
	// /dev/disk/by-id/ata-WDC_WD101EFBX-68B0AN0_VCJ3MYHP:
	// drive state is:  active/idle
	//
	// /dev/disk/by-id/ata-WDC_WD101EFBX-68B0AN0_VCJ3V79P:
	// drive state is:  active/idle

	matches := driveHdparmRegex.FindAllStringSubmatch(string(out), -1)

	data := map[string]string{}
	var loopErr []error
	for _, m := range matches {
		device := m[1]
		state := m[2]

		if !slices.Contains(driveHdparmDevices, device) {
			loopErr = append(loopErr, fmt.Errorf("invalid device found: %s", device))
			continue
		}
		if !slices.Contains(driveHdparmStates, state) {
			loopErr = append(loopErr, fmt.Errorf("invalid state %q for %s", state, device))
			continue
		}

		data[device] = state
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
		default: // explicit "unknown" from hdparm, or no entry parsed for this drive
			unknown = 1
		}

		driveActiveGauge.With(prometheus.Labels{"device": d}).Set(active)
		driveSleepingGauge.With(prometheus.Labels{"device": d}).Set(sleeping)
		driveStandbyGauge.With(prometheus.Labels{"device": d}).Set(standby)
		driveUnknownGauge.With(prometheus.Labels{"device": d}).Set(unknown)

	}

	if runErr != nil {
		return fmt.Errorf("hdparm returned non-zero: %s (output=%s)", runErr, strconv.Quote(string(out)))
	}
	if len(matches) == 0 {
		return fmt.Errorf("unable to match output, result is nil: %s", strconv.Quote(string(out)))
	}
	if len(loopErr) > 0 {
		var errorMsg []string
		for _, e := range loopErr {
			errorMsg = append(errorMsg, e.Error())
		}
		return fmt.Errorf("unable to hdparm output: %s", strings.Join(errorMsg, ", "))
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

	dir := "/var/log/jobs"

	entries, err := os.ReadDir(dir)
	if err != nil {
		return fmt.Errorf("unable to read jobs directory: %s", err)
	}

	var loopErr []error
	thisRun := map[string]struct{}{}
	for _, e := range entries {

		// /var/log/jobs is mode 0777 in prod; ignore anything that isn't a plain
		// job log file (subdirs, dotfiles, editor swap files) so a stray entry
		// can't tick exporter_errors every 5s.
		if !e.Type().IsRegular() || strings.HasPrefix(e.Name(), ".") {
			continue
		}

		// Track on file-presence (not parse-success) so a transiently malformed
		// log retains its previous gauge value; reaping is for jobs whose log
		// file has been removed entirely.
		thisRun[e.Name()] = struct{}{}

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

			dataTime, err := time.Parse(time.RFC3339, d[1])
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
				nextDataTime = dataTime.AddDate(0, 0, retry*1)
			case "weekly":
				nextDataTime = dataTime.AddDate(0, 0, retry*7)
			case "monthly":
				nextDataTime = dataTime.AddDate(0, retry*1, 0)
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

	for job := range lastCronJobs {
		if _, ok := thisRun[job]; !ok {
			cronLastSuccessTimestamp.DeleteLabelValues(job)
			cronNextRunTimestamp.DeleteLabelValues(job)
		}
	}
	lastCronJobs = thisRun

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

// Distinct gauges for distinct sources: NETDATA_CONTEXTS maps short names to
// metric contexts (data-pipeline health), NETDATA_COLLECTORS lists collector
// plugin ids (plugin-runtime health). Folding both into one metric let two
// writers stomp on each other when a short name happened to equal a plugin id.
var netdataContextUp = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "netdata_context_up",
		Help: "Set to 1 when the netdata metric context is live",
	},
	[]string{
		// short name from NETDATA_CONTEXTS, not the underlying context id
		"context",
	},
)
var netdataCollectorUp = prometheus.NewGaugeVec(
	prometheus.GaugeOpts{
		Name: "netdata_collector_up",
		Help: "Set to 1 when the netdata collector plugin is running",
	},
	[]string{
		// collector identifier
		"collector",
	},
)

type NetdataContext struct {
	Family     string
	Priority   int
	FirstEntry int
	LastEntry  int
	Live       bool
}

type NetdataContextsResponse struct {
	Contexts map[string]NetdataContext
}

// Tracks the job-log filenames observed on the previous scrape so we can
// DeleteLabelValues for jobs that have since been decommissioned. Without this
// the GaugeVec keeps emitting the last known value of cron_next_run_timestamp
// for a deleted job, and cron_job_missed eventually fires for a job that no
// longer exists.
var lastCronJobs = map[string]struct{}{}

var netdataContexts map[string]string

func getNetdataContextStatusInit() error {
	netdataContexts = make(map[string]string)

	value, ok := os.LookupEnv("NETDATA_CONTEXTS")
	if ok && value != "" {
		for _, v := range strings.Split(value, ",") {
			// SplitN keeps the context id intact even if it ever contains a
			// colon -- only the first separator should be consumed.
			vv := strings.SplitN(v, ":", 2)
			if len(vv) != 2 {
				return fmt.Errorf("invalid format for NETDATA_CONTEXTS env var, expected comma-separated name:context pairs, got: %s", v)
			}

			netdataContexts[vv[1]] = vv[0]
		}
	}
	return nil
}

func getNetdataContextStatus() error {

	if len(netdataContexts) == 0 {
		return nil
	}

	var resp NetdataContextsResponse
	err := getJSON("http://localhost:19999/api/v2/contexts", &resp)
	if err != nil {
		return err
	}

	metrics := map[string]float64{}

	for id, c := range resp.Contexts {
		name, ok := netdataContexts[id]
		if !ok {
			continue
		}

		if c.Live {
			metrics[name] = 1
		}
	}

	for _, name := range netdataContexts {
		value, ok := metrics[name]
		if !ok {
			value = 0
		}

		netdataContextUp.With(prometheus.Labels{"context": name}).Set(value)
	}

	return nil

}

type NetdataCollector struct {
	Type            string
	Status          string
	Sync            bool
	UserDisabled    bool
	RestartRequired bool
	PluginRejected  bool
}

type NetdataConfigResponse struct {
	Tree map[string]map[string]NetdataCollector
}

var netdataCollectors []string

func getNetdataCollectorStatusInit() {
	value, ok := os.LookupEnv("NETDATA_COLLECTORS")
	if ok && value != "" {
		netdataCollectors = strings.Split(value, ",")
	}
}

func getNetdataCollectorStatus() error {

	if len(netdataCollectors) == 0 {
		return nil
	}

	var resp NetdataConfigResponse
	err := getJSON("http://localhost:19999/api/v1/config", &resp)
	if err != nil {
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
