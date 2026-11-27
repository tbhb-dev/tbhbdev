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

func writeExecutable(t *testing.T, dir, name string, body []byte) string {
	t.Helper()

	path := filepath.Join(dir, name)
	//nolint:gosec // The test creates the directory and supplies fixture names.
	err := os.WriteFile(path, body, 0o700)
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

	front := writeExecutable(t, bundle, "tbhbdev", binary)

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
		//nolint:reassign // This subprocess replaces the test arguments before calling main.
		os.Args = append(os.Args[:1], os.Args[3:]...)

		main()

		return
	}

	t.Parallel()

	bundle, search := t.TempDir(), t.TempDir()
	link := linkFront(t, bundle, search)

	const (
		command  = "other"
		help     = "Usage: tbhbdev <command> [args...]\n       tbhbdev --version\n"
		stubName = "tbhbdev-py"
	)

	writeExecutable(t, search, stubName, nil)
	checkFront(t, link, nil, 0, help, "")
	checkFront(t, link, []string{"--help"}, 0, help, "")
	checkFront(t, link, []string{"--version"}, 0, "tbhbdev dev\n", "")
	checkFront(t, link, []string{command}, 127, "", "tbhbdev: tbhbdev-py: executable not found\n")

	writeExecutable(t, bundle, stubName, nil)
	checkFront(t, link, []string{command}, 126, "", "tbhbdev: exec format error\n")

	script := []byte(`#!/bin/sh
read -r input
printf '<%s>\n' "$TBHBDEV_PROG" "$TBHBDEV_BUNDLE_VERSION" "$FRONT_TEST_VALUE" "$input" "$@"
printf 'stderr\n' >&2
exit 23
`)
	writeExecutable(t, bundle, stubName, script)
	writeExecutable(t, bundle, "tbhbdev-"+command, nil)

	const output = "<tbhbdev>\n<dev>\n<kept>\n<stdin>\n<other>\n<two words>\n<>\n"

	checkFront(t, link, []string{command, "two words", ""}, 23, output, "stderr\n")
}
