// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package dispatch routes command lines without inspecting the filesystem.
package dispatch

const help = "Usage: tbhbdev <command> [args...]\n       tbhbdev --version\n"

// BuiltinText returns the front's help or version text, or an empty string when
// the arguments need command dispatch.
func BuiltinText(args []string, version string) string {
	if len(args) == 0 {
		return help
	}

	switch args[0] {
	case "-h", "--help":
		return help
	case "-V", "--version":
		return "tbhbdev " + version + "\n"
	default:
		return ""
	}
}
