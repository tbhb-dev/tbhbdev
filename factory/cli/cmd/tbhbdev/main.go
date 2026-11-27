// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Command tbhbdev runs built-in commands and delegates to the Python CLI.
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"syscall"

	"github.com/tbhb-dev/tbhbdev/factory/cli/internal/dispatch"
)

const (
	cannotExec  = 126
	missingExec = 127
	python      = "tbhbdev-py"
)

//nolint:gochecknoglobals // The linker can set the bundle version at build time.
var bundleVersion = "dev"

//nolint:gochecknoglobals // The front registers its command handlers here.
var commands = map[string]func([]string) int{}

func main() {
	executable, _ := os.Executable()
	os.Exit(run(os.Args[1:], executable))
}

func run(args []string, executable string) int {
	if output := dispatch.BuiltinText(args, bundleVersion); output != "" {
		_, _ = fmt.Fprint(os.Stdout, output)

		return 0
	}

	if len(args) > 0 {
		if command := commands[args[0]]; command != nil {
			return command(args[1:])
		}
	}

	path := pythonPath(bundleDir(executable))
	if path == "" {
		fmt.Fprintf(os.Stderr, "tbhbdev: %s: executable not found\n", python)

		return missingExec
	}

	env := slices.DeleteFunc(os.Environ(), func(entry string) bool {
		key, _, _ := strings.Cut(entry, "=")

		return key == "TBHBDEV_PROG" || key == "TBHBDEV_BUNDLE_VERSION"
	})
	env = append(env, "TBHBDEV_PROG=tbhbdev", "TBHBDEV_BUNDLE_VERSION="+bundleVersion)

	//nolint:gosec // The path is resolved in the bundle, and exec passes args without a shell.
	err := syscall.Exec(path, append([]string{path}, args...), env)
	fmt.Fprintf(os.Stderr, "tbhbdev: %v\n", err)

	return cannotExec
}

func bundleDir(executable string) string {
	if !filepath.IsAbs(executable) {
		return ""
	}

	resolved, err := filepath.EvalSymlinks(executable)
	if err != nil {
		return ""
	}

	return filepath.Dir(resolved)
}

func pythonPath(dir string) string {
	if dir == "" {
		return ""
	}

	path := filepath.Join(dir, python)

	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		return ""
	}

	return path
}
