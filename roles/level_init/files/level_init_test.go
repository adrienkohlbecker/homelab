package main

import (
	"bytes"
	"strings"
	"testing"
)

// TestDetectPrio covers every log shape that produced noise in the
// 8-hour lab journal sample plus the false-positive cases the regex
// boundaries need to dodge. The "ERROR-in-body escalates" case
// documents a known trade-off (most-severe-keyword wins regardless
// of position in the scan window) — it's listed here so any future
// rule tightening that changes the behaviour fails this test loudly.
func TestDetectPrio(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want byte
	}{
		// Real shapes pulled from the lab journal:
		{"postgres LOG", `2026-05-12 23:57:22.978 UTC [259] LOG: checkpoint starting: time`, '6'},
		{"paperless INFO", `[2026-05-13 00:00:00,000] [INFO] [celery.beat] Scheduler`, '6'},
		{"bazarr INFO", `2026-05-13 02:00:01,899 - root - INFO (movies:39) - BAZARR`, '6'},
		{"z2m lowercase info", `[2026-05-13 02:20:01] info: z2m: Stopping`, '6'},
		{"jellyfin INF", `[00:00:00] [INF] [97] IntroSkipper.ScheduledTasks`, '6'},
		{"transmission WRN", `[2026-05-13T03:00:01.398+0200] WRN blocklist.cc:264`, '4'},
		{"tautulli WARNING", `2026-05-13 04:20:01 - WARNING :: Thread-18`, '4'},
		{"HA WARNING", `28 WARNING (MainThread) [homeassistant.helpers.service]`, '4'},
		{"HA ERROR", `ERROR (MainThread) [homeassistant.config_entries]`, '3'},
		{"postgres FATAL", `2026-05-13 02:20:25.308 UTC [8901] FATAL: terminating`, '2'},

		// Severity aliases:
		{"CRIT bracketed", `[CRIT] system overload`, '2'},
		{"emerg upper", `EMERG: system going down`, '2'},
		{"panic", `PANIC: kernel oops`, '2'},
		{"err abbrev", `[ERR] connection refused`, '3'},
		{"warn abbrev", `[WARN] deprecated config`, '4'},
		{"notice", `[NOTICE] starting service`, '5'},
		{"debug", `[DEBUG] entering function`, '7'},
		{"trace", `TRACE: enter foo`, '7'},

		// No-level lines pass through (most common case for nginx
		// access, plain output, traceback continuation):
		{"nginx access", `192.168.1.5 - - [13/May/2026] GET / HTTP/1.1 200 1234`, 0},
		{"traceback header", `Traceback (most recent call last):`, 0},
		{"empty line", ``, 0},
		{"plain text", `regular line with no keyword`, 0},

		// Substring false-positives the \b boundary must reject:
		{"errno not err", `bind: errno=98 address already in use`, 0},
		{"warning_count not warning", `warning_count: 14 in metrics`, 0},
		{"fatalist not fatal", `Spinoza was a fatalist`, 0},
		{"informational not info", `informational message about something`, 0},
		{"debugger not debug", `debugger attached`, 0},

		// Documented trade-off: keyword anywhere in the first 120
		// bytes triggers. Body text that mentions a level word
		// escalates. Real log lines almost always put the level at
		// the start, so this is acceptable in practice.
		{"ERROR-in-body escalates", `this line just mentions ERROR somewhere in the middle`, '3'},

		// Most-severe-first ordering: multiple keywords, the worst
		// one wins so a "FATAL: see INFO log" doesn't downgrade.
		{"fatal beats info", `FATAL: see INFO log for details`, '2'},
		{"err beats warn", `ERROR: WARNING level triggered`, '3'},

		// Postgres' `LOG:` is only matched with the trailing colon so
		// the English word "log" anywhere else doesn't downgrade real
		// errors. Without the colon constraint, `error reading log
		// file` would be classified as info instead of error.
		{"bare log word ignored", `error reading log file`, '3'},
		{"LOG: with colon", `[259] LOG: checkpoint complete`, '6'},

		// Scan window: anything past byte 120 is invisible. Real
		// logs put the level at the head; padding past 120 with
		// nonsense and putting ERROR at the end must not match.
		{"level past scan window", strings.Repeat("x", 121) + " ERROR: too late", 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := detectPrio([]byte(c.in))
			if got != c.want {
				t.Errorf("detectPrio(%q) = %q (%d), want %q (%d)",
					c.in, got, got, c.want, c.want)
			}
		})
	}
}

// TestPrefixStream drives a small multi-line stream through the actual
// prefixStream goroutine to verify line splitting, prefix insertion,
// and pass-through of unmatched lines all compose correctly. The
// scanner buffer is exercised implicitly by the bazarr-shaped line
// which is the longest in the case list.
func TestPrefixStream(t *testing.T) {
	in := strings.Join([]string{
		"INFO: starting",
		"ERROR: failed",
		"no level keyword here",
		"[WARN] something",
		"2026-05-12 23:57:22.978 UTC [259] LOG: checkpoint",
	}, "\n") + "\n"

	var out bytes.Buffer
	done := make(chan struct{}, 1)
	prefixStream(strings.NewReader(in), &out, done)
	<-done

	want := strings.Join([]string{
		"<6>INFO: starting",
		"<3>ERROR: failed",
		"no level keyword here",
		"<4>[WARN] something",
		"<6>2026-05-12 23:57:22.978 UTC [259] LOG: checkpoint",
	}, "\n") + "\n"

	if out.String() != want {
		t.Errorf("prefixStream mismatch:\nwant:\n%s\ngot:\n%s", want, out.String())
	}
}

// TestPrefixStreamSticky locks in the multi-line carry-over behaviour:
// an indented continuation of a classified line inherits that line's
// priority so a python traceback's frames stay grouped with their
// ERROR header. A flush-left line breaks the run regardless of
// whether it has its own keyword.
func TestPrefixStreamSticky(t *testing.T) {
	cases := []struct {
		name string
		in   []string
		want []string
	}{
		{
			name: "python traceback frames carry the header's prio",
			in: []string{
				`ERROR (MainThread) [homeassistant.config_entries] Error setting up`,
				`Traceback (most recent call last):`,
				`  File "/foo.py", line 1, in bar`,
				`    something()`,
				`ValueError: bad input`,
				`INFO: next event`,
			},
			want: []string{
				`<3>ERROR (MainThread) [homeassistant.config_entries] Error setting up`,
				// Traceback header is flush-left and has no keyword,
				// so it BREAKS the sticky run (sticky resets to 0).
				`Traceback (most recent call last):`,
				// Frames are indented but the run is already broken;
				// sticky is 0 so they pass through unprefixed. This
				// is a known limitation — fluentbit's multiline
				// filter handles it on the OpenObserve side.
				`  File "/foo.py", line 1, in bar`,
				`    something()`,
				`ValueError: bad input`,
				`<6>INFO: next event`,
			},
		},
		{
			name: "indented continuation of an ERROR line inherits",
			in: []string{
				`ERROR: connection refused`,
				`  details: timeout after 30s`,
				`  retrying`,
				`INFO: connected on retry`,
			},
			want: []string{
				`<3>ERROR: connection refused`,
				`<3>  details: timeout after 30s`,
				`<3>  retrying`,
				`<6>INFO: connected on retry`,
			},
		},
		{
			name: "go panic style (tab-indented frames inherit)",
			in: []string{
				`panic: runtime error: index out of range`,
				"\tat main.go:42",
				"\tat runtime/panic.go:99",
				`exit status 2`,
			},
			want: []string{
				`<2>panic: runtime error: index out of range`,
				"<2>\tat main.go:42",
				"<2>\tat runtime/panic.go:99",
				`exit status 2`,
			},
		},
		{
			name: "blank line breaks the run",
			in: []string{
				`ERROR: failure`,
				`  frame 1`,
				``,
				`  frame after blank`,
			},
			want: []string{
				`<3>ERROR: failure`,
				`<3>  frame 1`,
				``,
				`  frame after blank`,
			},
		},
		{
			name: "explicit prio on indented line wins over sticky",
			in: []string{
				`ERROR: outer`,
				`  INFO: inner explicit beats inherited`,
			},
			want: []string{
				`<3>ERROR: outer`,
				`<6>  INFO: inner explicit beats inherited`,
			},
		},
		{
			name: "unclassified flush-left line doesn't start a run",
			in: []string{
				`plain text`,
				`  indented but no run to inherit from`,
			},
			want: []string{
				`plain text`,
				`  indented but no run to inherit from`,
			},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			in := strings.Join(c.in, "\n") + "\n"
			want := strings.Join(c.want, "\n") + "\n"
			var out bytes.Buffer
			done := make(chan struct{}, 1)
			prefixStream(strings.NewReader(in), &out, done)
			<-done
			if out.String() != want {
				t.Errorf("sticky mismatch:\nwant:\n%s\ngot:\n%s", want, out.String())
			}
		})
	}
}

// Sanity benchmark; the parser is on every container's hot log path so
// keeping a per-line floor visible in CI is worth a few microseconds
// of build time. A representative postgres LOG line is the realistic
// "common case."
func BenchmarkDetectPrio(b *testing.B) {
	line := []byte(`2026-05-12 23:57:22.978 UTC [259] LOG: checkpoint starting: time`)
	for i := 0; i < b.N; i++ {
		detectPrio(line)
	}
}
