// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Command tbhbdev dispatches to sibling Go commands and the Python CLI.
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
)

//nolint:gochecknoglobals // The linker can set the bundle version at build time.
var bundleVersion = "dev"

func main() {
	executable, _ := os.Executable()
	os.Exit(run(os.Args[1:], executable))
}

func run(args []string, executable string) int {
	dir := bundleDir(executable)
	name := dispatch.PluginName(args)

	path := launchPath(dir, name)
	if path == "" {
		name = ""
	}

	decision := dispatch.Decide(args, name, bundleVersion)
	switch decision.Action {
	case dispatch.Print:
		_, _ = fmt.Fprint(os.Stdout, decision.Text)

		return 0
	case dispatch.Fallback:
		path = launchPath(dir, decision.Name)
	case dispatch.Plugin:
	}

	if path == "" {
		fmt.Fprintf(os.Stderr, "tbhbdev: %s: executable not found\n", decision.Name)

		return missingExec
	}

	env := slices.DeleteFunc(os.Environ(), func(entry string) bool {
		key, _, _ := strings.Cut(entry, "=")

		return key == "TBHBDEV_PROG" || key == "TBHBDEV_BUNDLE_VERSION"
	})
	env = append(env, "TBHBDEV_PROG="+decision.Prog, "TBHBDEV_BUNDLE_VERSION="+bundleVersion)

	//nolint:gosec // The path is resolved in the bundle, and exec passes args without a shell.
	err := syscall.Exec(path, append([]string{path}, decision.Args...), env)
	fmt.Fprintf(os.Stderr, "%s: %v\n", decision.Prog, err)

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

func launchPath(dir, name string) string {
	if dir == "" || name == "" {
		return ""
	}

	path := filepath.Join(dir, name)
	//nolint:gosec // Dispatch only supplies a single filename or the fixed fallback name.
	info, err := os.Stat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0o111 == 0 {
		return ""
	}

	return path
}
