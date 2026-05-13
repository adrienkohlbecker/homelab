// level_init is a minimal PID-1 process for podman containers that
// interposes on the workload's stdout/stderr and prepends a BSD syslog
// priority prefix (<N>) based on a level keyword scanned out of the
// first 120 bytes of each line. With --log-driver=passthrough, podman
// hands the streams to the systemd unit, where journald's stream parser
// reads <N> and sets PRIORITY — fixing the "every line lands at err
// because podman hardcodes stderr=3" problem at the source.
//
// PID-1 duties: signal forwarding (catchable signals to the child's
// process group via the negative-pgid kill) and orphan reaping (a
// single Wait4(-1) loop consumes both the workload's exit status and
// any reparented orphans). In a podman container with --init, PID 1
// of the namespace is automatically the reaper for any orphan in the
// container's process tree — no PR_SET_CHILD_SUBREAPER dance needed.
// Exit-status follows shell convention: 128+signo on signalled exit,
// child's status otherwise.
//
// Multi-line errors (python/go/java tracebacks etc.) are handled by a
// "sticky" priority — once a line gets classified, subsequent indented
// lines inherit the same priority until a non-indented line breaks the
// run. This keeps the frames of a stack searchable via journalctl -p
// err along with the header.

package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"regexp"
	"syscall"
)

// Severity table walked most-severe-first; first match wins. Limiting
// the scan to the first 120 bytes keeps an "INFO" line that mentions
// "ERROR" later in its body from being escalated. \b is a word boundary
// in RE2, so [INFO]/INFO:/space-INFO-space all match while "errno" /
// "warning_count" / "fatalist" don't. Postgres-specific "LOG:" is
// matched as a separate rule because the bare word "log" is too common
// to use as a marker.
var sevTable = []struct {
	prio byte
	rx   *regexp.Regexp
}{
	{'2', regexp.MustCompile(`(?i)\b(fatal|panic|emerg)\b`)},
	{'2', regexp.MustCompile(`(?i)\b(crit|critical)\b`)},
	{'3', regexp.MustCompile(`(?i)\b(err|error)\b`)},
	{'4', regexp.MustCompile(`(?i)\b(warn|warning|wrn)\b`)},
	{'5', regexp.MustCompile(`(?i)\bnotice\b`)},
	{'6', regexp.MustCompile(`(?i)\b(info|inf)\b`)},
	{'6', regexp.MustCompile(`(?i)\blog:`)},
	{'7', regexp.MustCompile(`(?i)\b(debug|dbg|trace)\b`)},
}

const scanHead = 120

func detectPrio(line []byte) byte {
	head := line
	if len(head) > scanHead {
		head = head[:scanHead]
	}
	for _, e := range sevTable {
		if e.rx.Match(head) {
			return e.prio
		}
	}
	return 0
}

// isContinuation classifies a line as part of the previous line's
// logical message. We use leading whitespace as the heuristic: python
// frames are "  File ..." indented two spaces, go panics are "\t..."
// tab-indented, java exceptions are "\tat ..." likewise. Anything
// flush-left is treated as the start of a fresh logical record.
//
// This doesn't catch the trailing exception-class line in a python
// traceback ("ValueError: ...") which is flush-left — that ends up
// with whatever default priority systemd assigns. The OpenObserve
// path covers this gap via the fluent-bit multiline filter; for the
// local journal, surrounding context shows up when scrolling and is
// usually enough.
func isContinuation(line []byte) bool {
	return len(line) > 0 && (line[0] == ' ' || line[0] == '\t')
}

// prefixStream reads lines from r, scans the head for a level keyword,
// writes the line to w with a <N> prefix if matched, untouched otherwise.
// Multi-line errors carry the priority forward across indented
// continuations so a python traceback's File/frame lines stay grouped
// with their ERROR header in the journal. Scanner buffer is sized for
// 1MB lines — most apps emit much shorter, but a long stack trace or
// a binary blob in a log line shouldn't kill us.
func prefixStream(r io.Reader, w io.Writer, done chan<- struct{}) {
	defer func() { done <- struct{}{} }()
	s := bufio.NewScanner(r)
	s.Buffer(make([]byte, 64*1024), 1<<20)
	var sticky byte
	for s.Scan() {
		line := s.Bytes()
		p := detectPrio(line)
		if p == 0 && sticky != 0 && isContinuation(line) {
			// Inherit the previous classified line's priority for
			// this indented continuation. sticky stays unchanged.
			p = sticky
		} else {
			// Explicit detection OR a flush-left non-continuation
			// resets the run, even if the new line is itself
			// unclassified (which is what we want: a fresh
			// flush-left line shouldn't carry the prior priority).
			sticky = p
		}
		if p != 0 {
			fmt.Fprintf(w, "<%c>%s\n", p, line)
		} else {
			w.Write(line)
			w.Write([]byte{'\n'})
		}
	}
}

// waitAll blocks on Wait4(-1) collecting every terminated descendant.
// Orphans (grandchildren reparented to PID 1 after their direct parent
// exits) are reaped transparently; when the workload itself terminates
// its exit status is returned. Combining wait+reap in a single loop
// avoids the classic race where a separate SIGCHLD goroutine reaps the
// workload's status out from under exec.Cmd.Wait().
func waitAll(workloadPid int) int {
	for {
		var ws syscall.WaitStatus
		pid, err := syscall.Wait4(-1, &ws, 0, nil)
		if err == syscall.EINTR {
			continue
		}
		if err != nil {
			// ECHILD shouldn't happen while the workload is still
			// running; treat anything we can't wait on as a bug
			// and surface a generic failure rather than spin.
			fmt.Fprintln(os.Stderr, "level_init: wait4:", err)
			return 1
		}
		if pid == workloadPid {
			if ws.Signaled() {
				return 128 + int(ws.Signal())
			}
			return ws.ExitStatus()
		}
		// Orphan; status already collected, continue.
	}
}

func main() {
	// podman invokes init as `<init-path> -- <cmd> [args...]` when
	// --init is set; catatonit/tini swallow the leading "--" the same
	// way. Strip it so users running level_init outside podman aren't
	// forced to use the same convention.
	args := os.Args[1:]
	if len(args) > 0 && args[0] == "--" {
		args = args[1:]
	}
	if len(args) < 1 {
		fmt.Fprintln(os.Stderr, "usage: level_init [--] <cmd> [args...]")
		os.Exit(64)
	}

	cmd := exec.Command(args[0], args[1:]...)
	cmd.Stdin = os.Stdin
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}

	stdoutPipe, err := cmd.StdoutPipe()
	if err != nil {
		fmt.Fprintln(os.Stderr, "level_init: stdout pipe:", err)
		os.Exit(127)
	}
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		fmt.Fprintln(os.Stderr, "level_init: stderr pipe:", err)
		os.Exit(127)
	}

	if err := cmd.Start(); err != nil {
		fmt.Fprintln(os.Stderr, "level_init: exec:", err)
		os.Exit(127)
	}

	// Stream drainers complete when their pipe closes (on child
	// exit). We wait on these alongside cmd.Wait() so the last lines
	// of output are flushed before we exit.
	streamDone := make(chan struct{}, 2)
	go prefixStream(stdoutPipe, os.Stdout, streamDone)
	go prefixStream(stderrPipe, os.Stderr, streamDone)

	// Forward the signals that container workloads care about to the
	// child's process group. SIGKILL/SIGSTOP are uncatchable and the
	// runtime swallows them. SIGCHLD is consumed by waitAll's Wait4
	// loop, not forwarded. SIGURG is used by Go's runtime for goroutine
	// preemption — forwarding it would confuse the child.
	fwd := make(chan os.Signal, 8)
	signal.Notify(fwd,
		syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP,
		syscall.SIGQUIT, syscall.SIGUSR1, syscall.SIGUSR2,
	)
	go func() {
		for s := range fwd {
			syscall.Kill(-cmd.Process.Pid, s.(syscall.Signal))
		}
	}()

	exitCode := waitAll(cmd.Process.Pid)
	<-streamDone
	<-streamDone
	os.Exit(exitCode)
}
