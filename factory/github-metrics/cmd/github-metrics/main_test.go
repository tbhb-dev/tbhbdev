// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"

	"github.com/tbhb-dev/tbhbdev/factory/github-metrics/internal/recorder"
)

var errFloorRead = errors.New("floor read failed")

type stubLedger struct {
	floor    time.Time
	floorErr error
}

func (stubLedger) Created() time.Time                  { return time.Time{} }
func (s stubLedger) RepairFloor() (time.Time, error)   { return s.floor, s.floorErr }
func (stubLedger) ClaimMilestone(string) (bool, error) { return true, nil }

func TestLogRepairFloor(t *testing.T) {
	t.Parallel()

	var logs bytes.Buffer

	log := slog.New(slog.NewJSONHandler(&logs, nil))
	floor := time.Date(2026, 11, 15, 13, 0, 0, 0, time.UTC)

	err := logRepairFloor(log, stubLedger{floor: floor, floorErr: nil})
	if err != nil {
		t.Fatal(err)
	}

	var entry map[string]string

	err = json.Unmarshal(logs.Bytes(), &entry)
	if err != nil {
		t.Fatal(err)
	}

	if entry["level"] != "INFO" || entry["repair_floor"] != floor.Format(time.RFC3339) ||
		len(entry) != 4 {
		t.Fatalf("log entry = %v, want one info line with only the floor as an attribute", entry)
	}
}

func TestLogRepairFloorReadError(t *testing.T) {
	t.Parallel()

	var logs bytes.Buffer

	log := slog.New(slog.NewJSONHandler(&logs, nil))

	err := logRepairFloor(log, stubLedger{floor: time.Time{}, floorErr: errFloorRead})
	if !errors.Is(err, errFloorRead) || logs.Len() != 0 {
		t.Fatalf(
			"logRepairFloor error = %v, log = %q; want %v and no log",
			err,
			logs.String(),
			errFloorRead,
		)
	}
}

func TestNewRecorderWithoutLedger(t *testing.T) {
	t.Parallel()

	_, err := newRecorder("configured", nil, nil, slog.New(slog.DiscardHandler))
	if !errors.Is(err, recorder.ErrNoLedger) {
		t.Fatalf("newRecorder error = %v, want %v", err, recorder.ErrNoLedger)
	}
}

func TestNewRecorderWithoutEndpoint(t *testing.T) {
	t.Parallel()

	consumer, err := newRecorder("", nil, nil, slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}

	if _, ok := consumer.(noopRecorder); !ok {
		t.Fatalf("recorder has type %T, want noopRecorder", consumer)
	}

	err = consumer.Record(t.Context(), "ping", []byte(`{}`))
	if !errors.Is(err, errNoRecorder) {
		t.Fatalf("Record error = %v, want %v", err, errNoRecorder)
	}
}

func TestNewRecorderWithEndpoint(t *testing.T) {
	t.Parallel()

	reader := sdkmetric.NewManualReader()
	provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))

	t.Cleanup(func() {
		shutdownErr := provider.Shutdown(t.Context())
		if shutdownErr != nil {
			t.Error(shutdownErr)
		}
	})

	var ledger stubLedger

	consumer, err := newRecorder(
		"configured",
		provider.Meter("test"),
		ledger,
		slog.New(slog.DiscardHandler),
	)
	if err != nil {
		t.Fatal(err)
	}

	if _, ok := consumer.(*recorder.Recorder); !ok {
		t.Fatalf("recorder has type %T, want *recorder.Recorder", consumer)
	}

	err = consumer.Record(t.Context(), "ping", []byte(`{}`))
	if err != nil {
		t.Fatal(err)
	}

	checkEventCounter(t, reader)
}

func checkEventCounter(t *testing.T, reader *sdkmetric.ManualReader) {
	t.Helper()

	var data metricdata.ResourceMetrics

	err := reader.Collect(t.Context(), &data)
	if err != nil {
		t.Fatal(err)
	}

	if len(data.ScopeMetrics) != 1 || len(data.ScopeMetrics[0].Metrics) == 0 {
		t.Fatalf("scope metrics = %v, want counter observations", data.ScopeMetrics)
	}

	measurement := data.ScopeMetrics[0].Metrics[0]
	sum, ok := measurement.Data.(metricdata.Sum[int64])

	if measurement.Name != "github.events" || !ok || len(sum.DataPoints) != 1 ||
		sum.DataPoints[0].Value != 1 {
		t.Fatalf("metric = %v, want one github.events observation", measurement)
	}
}

func TestRunWithoutReconciler(t *testing.T) {
	for _, name := range []string{appIDEnv, installationIDEnv, appKeyFileEnv, apiURLEnv, "OTEL_EXPORTER_OTLP_ENDPOINT"} {
		t.Setenv(name, "")
	}

	dir := t.TempDir()
	secretFile := filepath.Join(dir, "secret")
	t.Setenv("WEBHOOK_SECRET_FILE", secretFile)
	t.Setenv("STORE_FILE", filepath.Join(dir, "store.db"))

	err := os.WriteFile(secretFile, []byte("test secret"), 0o600)
	if err != nil {
		t.Fatal(err)
	}

	address := httptest.NewUnstartedServer(nil)
	addr := address.Listener.Addr().String()
	address.Close()

	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()

	var logs bytes.Buffer

	log := slog.New(slog.NewJSONHandler(&logs, nil))
	done := make(chan error, 1)

	go func() { done <- run(ctx, addr, log) }()

	ticker := time.NewTicker(10 * time.Millisecond)
	defer ticker.Stop()

	for probe(addr) != 0 {
		select {
		case err = <-done:
			t.Fatalf("service exited before it was healthy: %v", err)
		case <-ctx.Done():
			t.Fatalf("service did not become healthy, shutdown error = %v", <-done)
		case <-ticker.C:
		}
	}

	cancel()

	err = <-done
	if err != nil {
		t.Fatal(err)
	}

	if bytes.Count(logs.Bytes(), []byte(`"level":"INFO","msg":"reconciler disabled"`)) != 1 {
		t.Fatalf("want one INFO line for the disabled reconciler, got %s", logs.String())
	}
}

func TestReconcilerConfigDisabled(t *testing.T) {
	t.Parallel()

	env := map[string]string{apiURLEnv: defaultGitHubAPI}

	var disabled reconcileConfig

	cfg, err := reconcilerConfig(func(name string) string { return env[name] })
	if err != nil || cfg != disabled {
		t.Fatalf("want disabled configuration, got error %v", err)
	}
}

func TestReconcilerConfigPartial(t *testing.T) {
	t.Parallel()

	for _, test := range []struct{ set, missing string }{
		{appIDEnv, installationIDEnv + ", " + appKeyFileEnv},
		{installationIDEnv, appIDEnv + ", " + appKeyFileEnv},
		{appKeyFileEnv, appIDEnv + ", " + installationIDEnv},
	} {
		env := map[string]string{test.set: "test private value"}
		want := errMissingReconcilerConfig.Error() + ": " + test.missing

		_, err := reconcilerConfig(func(name string) string { return env[name] })
		if !errors.Is(err, errMissingReconcilerConfig) || err.Error() != want {
			t.Errorf("with %s set, want an error naming only %s", test.set, test.missing)
		}
	}
}

func TestReconcilerConfigInvalidIDs(t *testing.T) {
	t.Parallel()

	const invalid = "test private value"

	for name, want := range map[string]error{appIDEnv: errInvalidAppID, installationIDEnv: errInvalidInstallationID} {
		env := map[string]string{
			appIDEnv:          "1",
			installationIDEnv: "2",
			appKeyFileEnv:     "test-key.pem",
		}
		env[name] = invalid

		_, err := reconcilerConfig(func(name string) string { return env[name] })
		if !errors.Is(err, want) || err.Error() != want.Error() {
			t.Errorf("want an error naming %s without its value", name)
		}
	}
}

func TestReconcilerConfigComplete(t *testing.T) {
	t.Parallel()

	const keyFile = "test-key.pem"

	for _, apiURL := range []string{"", defaultGitHubAPI + "/api/v3"} {
		env := map[string]string{
			appIDEnv:          "1",
			installationIDEnv: "2",
			appKeyFileEnv:     keyFile,
			apiURLEnv:         apiURL,
		}

		cfg, err := reconcilerConfig(func(name string) string { return env[name] })
		if err != nil {
			t.Fatal(err)
		}

		wantURL := apiURL
		if wantURL == "" {
			wantURL = defaultGitHubAPI
		}

		want := reconcileConfig{keyFile: keyFile, apiURL: wantURL, appID: 1, installationID: 2}
		if cfg != want {
			t.Fatalf("config = %+v, want %+v", cfg, want)
		}
	}
}

func TestCIBackfillConfig(t *testing.T) {
	t.Parallel()

	for value, days := range map[string]int{"": 0, "o/r:7": 7, "o/r:1": 1} {
		cfg, err := ciBackfillConfig(func(string) string { return value })
		repository := "o/r"

		if days == 0 {
			repository = ""
		}

		if err != nil || cfg.Repository != repository || cfg.Days != days {
			t.Fatalf("config for %q = %+v, error = %v", value, cfg, err)
		}
	}

	for _, value := range []string{
		"o/r:8", "o/r:0", "o/r:-1", "o/r:day", "o/r", "o:7", "o/r/extra:7", "o/..:7", "o/r:7\n",
	} {
		_, err := ciBackfillConfig(func(string) string { return value })
		if err == nil ||
			err.Error() != "GITHUB_CI_BACKFILL must name a repository and days from 1 to 7" {
			t.Fatalf("expected redacted variable error, got %v", err)
		}
	}
}
