// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

// cspell:words awrel

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"

	"github.com/tbhb-dev/tbhbdev/factory/event-gateway/internal/filter"
)

const (
	mcpStateTestSubscription = `{"provider":"github","event":"issues","repository":"owner/repo","number":[2]}`
	mcpStateTestKey          = "fixture-session"
)

func mcpStateDocument(subscriptions string) string {
	return `{"subscriptions":[` + subscriptions + `],"version":1}`
}

func setMCPStateKeys(t *testing.T, keys [3]string) {
	t.Helper()

	for i, name := range [...]string{mcpSessionKeyEnv, awrelSessionEnv, claudeCodeSessionEnv} {
		t.Setenv(name, keys[i])
	}
}

func TestMCPStateLocation(t *testing.T) {
	dir, home := t.TempDir(), t.TempDir()
	fallback := filepath.Join(home, ".local", "state")

	const awrelKey = "fixture-awrel-session"

	for _, test := range []struct {
		keys                [3]string
		xdg, home, dir, key string
		warning             string
	}{
		{[3]string{"../first", awrelKey, mcpStateTestKey}, dir, home, dir, "../first", mcpStateReadWarning},
		{[3]string{"", awrelKey, mcpStateTestKey}, "relative", home, fallback, awrelKey, mcpStateReadWarning},
		{[3]string{"", "", mcpStateTestKey}, "", home, fallback, mcpStateTestKey, mcpStateReadWarning},
		{[3]string{"", "", mcpStateTestKey}, "relative", "", "", "", mcpStateHomeWarning},
	} {
		t.Setenv(mcpStateHomeEnv, test.xdg)
		t.Setenv("HOME", test.home)
		setMCPStateKeys(t, test.keys)

		var logs bytes.Buffer

		subs := loadMCPSubscriptions(t.Context(), slog.New(slog.NewTextHandler(&logs, nil)))
		checkEqual(t, subs.state.dir, test.dir)

		if test.key != "" {
			want := fmt.Sprintf("%x.json", sha256.Sum256([]byte(test.key)))
			checkEqual(t, subs.state.file, filepath.Join("event-gateway", "sessions", want))
		} else {
			checkEqual(t, subs.state.file, "")
		}

		checkMCPStateLog(t, &logs, test.warning)
	}

	t.Setenv(mcpStateHomeEnv, dir)
	setMCPStateKeys(t, [3]string{})

	sub, err := filter.Parse([]byte(mcpStateTestSubscription))
	checkError(t, err)

	var logs bytes.Buffer

	subs := loadMCPSubscriptions(t.Context(), slog.New(slog.NewTextHandler(&logs, nil)))
	checkMCPStateLog(t, &logs, "")
	checkEqual(t, subs.state.file, "")
	checkEqual(t, subs.set(sub, true), true)
	checkEqual(t, len(subs.list()), 1)

	entries, err := os.ReadDir(dir)
	checkError(t, err)
	checkEqual(t, len(entries), 0)
}

func TestMCPStateLoad(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(mcpStateHomeEnv, dir)
	setMCPStateKeys(t, [3]string{mcpStateTestKey, "", ""})

	empty := loadMCPSubscriptions(t.Context(), slog.New(slog.DiscardHandler))
	path := filepath.Join(dir, empty.state.file)
	checkError(t, os.MkdirAll(path, 0o700))

	var logs bytes.Buffer

	logger := slog.New(slog.NewTextHandler(&logs, nil))
	checkEqual(t, len(loadMCPSubscriptions(t.Context(), logger).list()), 0)
	checkMCPStateLog(t, &logs, mcpStateReadWarning)
	checkError(t, os.Remove(path))

	mixed := mcpStateTestSubscription + `,{"provider":"bad/provider"},null,` + mcpStateTestSubscription
	for _, test := range []struct {
		data    string
		warning string
		count   int
	}{
		{"", mcpStateReadWarning, 0},
		{`{"version":1,"subscriptions":[` + mcpStateTestSubscription + "]", mcpStateReadWarning, 0},
		{`{"version":2,"subscriptions":[` + mcpStateTestSubscription + "]}", mcpStateReadWarning, 0},
		{`{"version":1,"subscriptions":null}`, mcpStateReadWarning, 0},
		{`{"version":1,"subscriptions":[],"cursor":-1}`, mcpStateReadWarning, 0},
		{mcpStateDocument(mcpStateTestSubscription), "", 1},
		{`{"cursor":5,"version":1,"subscriptions":[` + mixed + `]}`, mcpStateReadWarning, 1},
	} {
		checkError(t, os.WriteFile(path, []byte(test.data), 0o600))
		got := loadMCPSubscriptions(t.Context(), logger)
		subs, cursor := got.snapshot()
		checkEqual(t, len(subs), test.count)
		checkEqual(t, cursor, nil)
		checkMCPStateLog(t, &logs, test.warning)
	}
}

func awrelStatePaths(t *testing.T) (string, string) {
	t.Helper()

	dir := t.TempDir()
	t.Setenv(mcpStateHomeEnv, dir)
	setMCPStateKeys(t, [3]string{"", mcpStateTestKey, ""})
	file := filepath.Join("sessions", fmt.Sprintf("%x.json", sha256.Sum256([]byte(mcpStateTestKey))))
	legacy := filepath.Join(dir, "awrel", file)
	checkError(t, os.MkdirAll(filepath.Dir(legacy), 0o700))

	return filepath.Join(dir, "event-gateway", file), legacy
}

func mcpStateTestSubscriptions(count int) string {
	items := make([]string, count)
	for i := range items {
		items[i] = strings.Replace(mcpStateTestSubscription, "owner/repo", "owner/repo"+strconv.Itoa(i), 1)
	}

	return strings.Join(items, ",")
}

func TestMCPStateAwrelImport(t *testing.T) {
	own, legacy := awrelStatePaths(t)
	t.Setenv(gatewayTokenEnv, mcpFixture)

	document := `{"endpoint":"wss://example.invalid/old",` + mcpStateDocument(mcpStateTestSubscription)[1:]

	var logs bytes.Buffer

	logger := slog.New(slog.NewTextHandler(&logs, nil))
	checkEqual(t, loadMCPSubscriptions(t.Context(), logger).imported, false)
	checkMCPStateLog(t, &logs, mcpStateReadWarning)
	checkError(t, os.WriteFile(legacy, []byte(document), 0o600))
	subs := loadMCPSubscriptions(t.Context(), logger)
	checkEqual(t, subs.imported, true)
	checkEqual(t, len(subs.list()), 1)
	checkMCPStateLog(t, &logs, "")

	data, err := os.ReadFile(own)
	checkError(t, err)
	checkEqual(t, string(data), mcpStateDocument(mcpStateTestSubscription))
	data, err = os.ReadFile(legacy)
	checkError(t, err)
	checkEqual(t, string(data), document)

	checkError(t, os.WriteFile(legacy, []byte("invalid"), 0o600))
	subs = loadMCPSubscriptions(t.Context(), logger)
	checkEqual(t, subs.imported, false)
	checkEqual(t, len(subs.list()), 1)
	checkMCPStateLog(t, &logs, "")
}

func TestMCPStateAwrelSkip(t *testing.T) {
	own, legacy := awrelStatePaths(t)
	checkError(t, os.WriteFile(legacy, []byte(mcpStateDocument(mcpStateTestSubscription)), 0o600))

	var logs bytes.Buffer

	logger := slog.New(slog.NewTextHandler(&logs, nil))

	setMCPStateKeys(t, [3]string{})

	subs := loadMCPSubscriptions(t.Context(), logger)
	checkEqual(t, len(subs.list()), 0)
	checkEqual(t, subs.imported, false)
	checkMCPStateLog(t, &logs, "")

	_, err := os.Stat(own)
	checkEqual(t, os.IsNotExist(err), true)

	setMCPStateKeys(t, [3]string{mcpStateTestKey, "", ""})
	checkError(t, os.MkdirAll(own, 0o700))
	subs = loadMCPSubscriptions(t.Context(), logger)
	checkEqual(t, subs.imported, false)
	checkMCPStateLog(t, &logs, mcpStateReadWarning)
	checkError(t, os.Remove(own))

	for _, test := range [][2]string{
		{"invalid", mcpStateReadWarning},
		{`{"version":2,"subscriptions":[]}`, mcpStateReadWarning},
		{mcpStateDocument(""), ""},
	} {
		checkError(t, os.WriteFile(own, []byte(test[0]), 0o600))
		subs = loadMCPSubscriptions(t.Context(), logger)
		checkEqual(t, len(subs.list()), 0)
		checkEqual(t, subs.imported, false)
		checkMCPStateLog(t, &logs, test[1])

		data, readErr := os.ReadFile(own)
		checkError(t, readErr)
		checkEqual(t, string(data), test[0])
	}
}

func TestMCPStateAwrelInvalid(t *testing.T) {
	valid := mcpStateDocument(mcpStateTestSubscription)
	for _, test := range []struct {
		name, document string
		count          int
	}{
		{"malformed", `{"version":1,"endpoint":"wss://example.invalid/old"`, 0},
		{"version", strings.Replace(valid, `"version":1`, `"version":2`, 1), 0},
		{"missing", `{"version":1}`, 0},
		{"empty subscriptions", mcpStateDocument(""), 0},
		{"oversized", valid + strings.Repeat(" ", awrelStateLimit+1-len(valid)), 0},
		{"unreadable", "", 0},
		{"invalid subscription", mcpStateDocument(`null,{"provider":"bad/provider"}`), 0},
		{"partial", mcpStateDocument(`null,{"provider":"bad/provider"},` + mcpStateTestSubscription), 1},
		{"too many", mcpStateDocument(mcpStateTestSubscriptions(subscriptionLimit + 1)), subscriptionLimit},
	} {
		t.Run(test.name, func(t *testing.T) {
			own, legacy := awrelStatePaths(t)
			if test.document == "" {
				checkError(t, os.Mkdir(legacy, 0o700))
			} else {
				checkError(t, os.WriteFile(legacy, []byte(test.document), 0o600))
			}

			var logs bytes.Buffer

			subs := loadMCPSubscriptions(t.Context(), slog.New(slog.NewTextHandler(&logs, nil)))
			checkEqual(t, len(subs.list()), test.count)
			checkEqual(t, subs.imported, test.count > 0)
			checkMCPStateLog(t, &logs, awrelStateWarning)

			data, err := os.ReadFile(own)

			switch test.count {
			case 0:
				checkEqual(t, os.IsNotExist(err), true)
			case 1:
				checkError(t, err)
				checkEqual(t, string(data), valid)
			default:
				checkError(t, err)
				checkEqual(t, string(data), mcpStateDocument(mcpStateTestSubscriptions(subscriptionLimit)))
			}
		})
	}
}

func TestMCPStateAwrelReadFailure(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	file := filepath.Join(dir, "outside.json")
	root := filepath.Join(dir, "awrel")

	checkError(t, os.WriteFile(file, []byte(mcpStateDocument(mcpStateTestSubscription)), 0o600))
	checkError(t, os.Mkdir(root, 0o700))

	for _, test := range [][2]string{{file, "session.json"}, {root, filepath.Join("..", filepath.Base(file))}} {
		var logs bytes.Buffer

		logger := slog.New(slog.NewTextHandler(&logs, nil))
		subs := new(mcpSubscriptions)
		checkEqual(t, loadAwrelSubscriptions(t.Context(), logger, test[0], test[1], subs), true)
		checkEqual(t, subs.imported, false)
		checkEqual(t, len(subs.list()), 0)
		checkMCPStateLog(t, &logs, awrelStateWarning)
	}
}

func TestMCPStateTools(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "state")
	t.Setenv(mcpStateHomeEnv, dir)
	setMCPStateKeys(t, [3]string{mcpStateTestKey, "", ""})
	t.Setenv(gatewayTokenEnv, mcpFixture)
	t.Setenv(gatewayURLEnv, "ws://example.invalid")

	var logs bytes.Buffer

	logger := slog.New(slog.NewTextHandler(&logs, nil))
	subs := loadMCPSubscriptions(t.Context(), logger)
	checkMCPStateLog(t, &logs, mcpStateReadWarning)
	session, _, _ := connectMCPWithSubscriptions(t, subs, "test", nil)
	path := filepath.Join(dir, subs.state.file)

	for _, test := range [][2]string{
		{subscribeTool, mcpStateTestSubscription}, {unsubscribeTool, ""},
	} {
		checkEqual(t, callMCPTool(t, session, test[0], mcpStateTestSubscription).IsError, false)

		data, err := os.ReadFile(path)
		checkError(t, err)
		checkEqual(t, string(data), mcpStateDocument(test[1]))
		checkEqual(t, bytes.Contains(data, []byte(mcpFixture)), false)
	}

	checkMCPStateLog(t, &logs, "")
	checkMCPStateMode(t, path, 0o600)
	checkMCPStateMode(t, filepath.Dir(path), 0o700)
	checkMCPStateMode(t, filepath.Dir(filepath.Dir(path)), 0o700)
	checkMCPStateMode(t, dir, 0o700)

	entries, err := os.ReadDir(filepath.Dir(path))
	checkError(t, err)
	checkEqual(t, len(entries), 1)
}

func TestMCPStateSaveFailure(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(mcpStateHomeEnv, dir)
	setMCPStateKeys(t, [3]string{mcpStateTestKey, "", ""})

	var logs bytes.Buffer

	subs := loadMCPSubscriptions(t.Context(), slog.New(slog.NewTextHandler(&logs, nil)))
	checkMCPStateLog(t, &logs, mcpStateReadWarning)

	path := filepath.Join(dir, subs.state.file)
	blocker := filepath.Join(dir, "blocker")
	checkError(t, os.WriteFile(blocker, nil, 0o600))

	blocked := subs.state
	blocked.dir = blocker
	checkEqual(t, blocked.write(nil, nil), false)

	blocked.dir = dir
	blocked.file = filepath.Join("blocker", filepath.Base(path))
	checkEqual(t, blocked.write(nil, nil), false)
	checkError(t, os.MkdirAll(path, 0o700))

	session, _, _ := connectMCPWithSubscriptions(t, subs, "test", nil)
	checkEqual(t, callMCPTool(t, session, subscribeTool, mcpStateTestSubscription).IsError, false)
	checkMCPStateLog(t, &logs, mcpStateWriteWarning)
	checkEqual(t, len(subs.list()), 1)
	checkEqual(t, callMCPTool(t, session, listSubscriptionsTool, "").IsError, false)

	entries, err := os.ReadDir(filepath.Dir(path))
	checkError(t, err)
	checkEqual(t, len(entries), 1)
	checkEqual(t, entries[0].IsDir(), true)
	checkError(t, os.Remove(path))
	checkEqual(t, callMCPTool(t, session, unsubscribeTool, mcpStateTestSubscription).IsError, false)
	checkMCPStateLog(t, &logs, "")
}

func TestMCPStateCursorWriteFailure(t *testing.T) {
	t.Setenv(mcpStateHomeEnv, t.TempDir())
	setMCPStateKeys(t, [3]string{mcpStateTestKey, "", ""})

	var logs bytes.Buffer

	subs := loadMCPSubscriptions(t.Context(), slog.New(slog.NewTextHandler(&logs, nil)))
	checkMCPStateLog(t, &logs, mcpStateReadWarning)

	subs.state.writeFile = func(*os.File, []byte) (int, error) { return 0, io.ErrShortWrite }

	var deliveries gatewayDeliveries

	deliveries.ids = make(map[string]struct{})
	frame := sessionJSON[sessionObject]([]byte(gatewayTestEvent(7, "github", "id")))
	forwardGatewayFrame(frame, subs, &deliveries, func(channelEvent) bool { return false })
	_, cursor := subs.snapshot()
	checkEqual(t, cursor, nil)
	checkMCPStateLog(t, &logs, "")

	delivered := false

	forwardGatewayFrame(frame, subs, &deliveries, func(channelEvent) bool {
		_, before := subs.snapshot()
		checkEqual(t, before, nil)

		delivered = true

		return true
	})
	checkEqual(t, delivered, true)

	_, cursor = subs.snapshot()
	checkEqual(t, cursor != nil, true)
	checkEqual(t, *cursor, uint64(7))
	checkMCPStateLog(t, &logs, mcpStateWriteWarning)
}

func TestMCPStateTempWriteFailure(t *testing.T) {
	t.Parallel()

	root, err := os.OpenRoot(t.TempDir())
	checkError(t, err)
	t.Cleanup(func() { checkError(t, root.Close()) })

	const temp = "temp"

	file, err := root.OpenFile(temp, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	checkError(t, err)

	write := func([]byte) (int, error) { return 0, io.ErrShortWrite }
	checkEqual(t, replaceMCPState(root, file, temp, "target", []byte("data"), write), false)
	_, err = root.Stat(temp)
	checkEqual(t, os.IsNotExist(err), true)
}

func checkMCPStateMode(t *testing.T, path string, mode os.FileMode) {
	t.Helper()

	if runtime.GOOS == "windows" {
		return
	}

	info, err := os.Stat(path)
	checkError(t, err)
	checkEqual(t, info.Mode().Perm(), mode)
}

func checkMCPStateLog(t *testing.T, logs *bytes.Buffer, warning string) {
	t.Helper()

	got := logs.String()
	logs.Reset()

	if warning == "" {
		checkEqual(t, got, "")

		return
	}

	_, message, found := strings.Cut(got, " level=WARN msg=")
	checkEqual(t, found, true)
	checkEqual(t, message, fmt.Sprintf("%q\n", warning))
}
