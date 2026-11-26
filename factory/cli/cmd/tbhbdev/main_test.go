// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package main

import (
	"bytes"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func writeFile(t *testing.T, dir, name string, body []byte, mode os.FileMode) string {
	t.Helper()

	path := filepath.Join(dir, name)
	//nolint:gosec // The test creates the directory and supplies fixture names.
	err := os.WriteFile(path, body, mode)
	if err != nil {
		t.Fatal(err)
	}

	return path
}

// linkFront places a copy of the test executable in the bundle and links to it.
func linkFront(t *testing.T, bundle, search string) string {
	t.Helper()

	executable, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}

	//nolint:gosec // The OS provides the path to this test executable.
	binary, err := os.ReadFile(executable)
	if err != nil {
		t.Fatal(err)
	}

	front := writeFile(t, bundle, "tbhbdev", binary, 0o700)

	link := filepath.Join(search, "tbhbdev")

	err = os.Symlink(front, link)
	if err != nil {
		t.Fatal(err)
	}

	return link
}

func checkFront(t *testing.T, link string, args []string, status int, wantOut, wantErr string) {
	t.Helper()

	childArgs := append([]string{"-test.run=^TestFront$", "--"}, args...)
	//nolint:gosec // The executable and arguments come from test fixtures.
	cmd := exec.CommandContext(t.Context(), link, childArgs...)
	cmd.Dir = filepath.Dir(link)
	cmd.Env = append(cmd.Environ(), "PATH="+cmd.Dir, "TBHBDEV_TEST_FRONT=1",
		"TBHBDEV_PROG=old", "TBHBDEV_BUNDLE_VERSION=old", "FRONT_TEST_VALUE=kept")
	cmd.Stdin = strings.NewReader("stdin\n")

	var stderr bytes.Buffer

	cmd.Stderr = &stderr
	out, err := cmd.Output()

	var exit *exec.ExitError
	if err != nil && !errors.As(err, &exit) {
		t.Fatal(err)
	}

	code, stdout, diagnostic := cmd.ProcessState.ExitCode(), string(out), stderr.String()
	if code != status || stdout != wantOut || diagnostic != wantErr {
		t.Errorf("tbhbdev %q = (%d, %q, %q)", args, code, stdout, diagnostic)
		t.Logf("want (%d, %q, %q)", status, wantOut, wantErr)
	}
}

func TestFront(t *testing.T) {
	if os.Getenv("TBHBDEV_TEST_FRONT") == "1" {
		os.Args = append(os.Args[:1], os.Args[3:]...)

		main()

		return
	}

	t.Parallel()

	bundle, search := t.TempDir(), t.TempDir()
	link := linkFront(t, bundle, search)

	writeFile(t, search, "tbhbdev-other", nil, 0o700)
	writeFile(t, search, "tbhbdev-py", nil, 0o700)
	checkFront(t, link, []string{"--version"}, 0, "tbhbdev dev\n", "")
	checkFront(t, link, []string{"other"}, 127, "", "tbhbdev: tbhbdev-py: executable not found\n")

	script := []byte(`#!/bin/sh
read -r input
printf '<%s>\n' "$TBHBDEV_PROG" "$TBHBDEV_BUNDLE_VERSION" "$FRONT_TEST_VALUE" "$input" "$@"
printf 'stderr\n' >&2
exit 23
`)
	writeFile(t, bundle, "tbhbdev-py", script, 0o700)
	writeFile(t, bundle, "tbhbdev-a-b", script, 0o700)
	writeFile(t, bundle, "tbhbdev-a", nil, 0o700)
	writeFile(t, bundle, "tbhbdev-other", nil, 0o600)

	const (
		inherited = "\n<dev>\n<kept>\n<stdin>\n"
		stderr    = "stderr\n"
		pluginOut = "<tbhbdev a-b>" + inherited + "<two words>\n<>\n"
	)

	checkFront(t, link, []string{"a", "b"}, 126, "", "tbhbdev a: exec format error\n")
	checkFront(t, link, []string{"a-b", "two words", ""}, 23, pluginOut, stderr)
	checkFront(t, link, []string{"other", ""}, 23, "<tbhbdev>"+inherited+"<other>\n<>\n", stderr)
	checkFront(t, link, []string{"py", ""}, 23, "<tbhbdev>"+inherited+"<py>\n<>\n", stderr)
}
