// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

// Package dispatch routes command lines without inspecting the filesystem.
package dispatch

import "strings"

// Action tells the front how to handle a command line.
type Action int

// A route falls back to Python, launches a plugin, or prints built-in text.
const (
	Fallback Action = iota
	Plugin
	Print
)

// Decision describes the command to run or the text to print.
type Decision struct {
	Action Action
	Name   string
	Args   []string
	Prog   string
	Text   string
}

// Python is the console script for commands still implemented in Python.
const Python = "tbhbdev-py"

const help = "Usage: tbhbdev <command> [args...]\n       tbhbdev --version\n"

// PluginName gives the candidate executable name for the first argument.
func PluginName(args []string) string {
	if len(args) == 0 {
		return ""
	}

	word := args[0]
	if word == "" || word == "py" || strings.HasPrefix(word, "-") ||
		strings.ContainsAny(word, "/\\") {
		return ""
	}

	return "tbhbdev-" + word
}

// Decide routes args given the name of the executable plugin, if available.
func Decide(args []string, available, version string) Decision {
	if len(args) == 0 {
		return Decision{Action: Print, Text: help, Name: "", Args: nil, Prog: ""}
	}

	switch args[0] {
	case "-h", "--help":
		return Decision{Action: Print, Text: help, Name: "", Args: nil, Prog: ""}
	case "-V", "--version":
		return Decision{
			Action: Print,
			Text:   "tbhbdev " + version + "\n",
			Name:   "",
			Args:   nil,
			Prog:   "",
		}
	}

	if available != "" && available == PluginName(args) {
		return Decision{
			Action: Plugin,
			Name:   available,
			Args:   args[1:],
			Prog:   "tbhbdev " + args[0],
			Text:   "",
		}
	}

	return Decision{Action: Fallback, Name: Python, Args: args, Prog: "tbhbdev", Text: ""}
}
