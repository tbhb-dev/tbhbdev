// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package dispatch_test

import (
	"reflect"
	"testing"

	"github.com/tbhb-dev/tbhbdev/factory/cli/internal/dispatch"
)

func TestDecide(t *testing.T) {
	t.Parallel()

	const (
		helpFlag = "--help"
		pluginA  = "tbhbdev-a"
		pluginAB = "tbhbdev-a-b"
		front    = "tbhbdev"
	)

	for _, test := range []struct {
		name      string
		args      []string
		candidate string
		available string
		want      dispatch.Decision
	}{
		{"first word", []string{"a", "b", ""}, pluginA, pluginA, dispatch.Decision{
			Action: dispatch.Plugin, Name: pluginA, Args: []string{"b", ""}, Prog: "tbhbdev a", Text: "",
		}},
		{"hyphenated word", []string{"a-b", helpFlag}, pluginAB, pluginAB, dispatch.Decision{
			Action: dispatch.Plugin, Name: pluginAB, Args: []string{helpFlag}, Prog: "tbhbdev a-b", Text: "",
		}},
		{"no nesting", []string{"a", "b"}, pluginA, pluginAB, dispatch.Decision{
			Action: dispatch.Fallback, Name: dispatch.Python, Args: []string{"a", "b"}, Prog: front, Text: "",
		}},
		{"empty argument", []string{""}, "", "", dispatch.Decision{
			Action: dispatch.Fallback, Name: dispatch.Python, Args: []string{""}, Prog: front, Text: "",
		}},
		{"reserved name", []string{"py"}, "", dispatch.Python, dispatch.Decision{
			Action: dispatch.Fallback, Name: dispatch.Python, Args: []string{"py"}, Prog: front, Text: "",
		}},
		{"leading flag", []string{"--repo"}, "", "", dispatch.Decision{
			Action: dispatch.Fallback, Name: dispatch.Python, Args: []string{"--repo"}, Prog: front, Text: "",
		}},
		{"slash", []string{"a/b"}, "", "", dispatch.Decision{
			Action: dispatch.Fallback, Name: dispatch.Python, Args: []string{"a/b"}, Prog: front, Text: "",
		}},
		{"backslash", []string{`a\b`}, "", "", dispatch.Decision{
			Action: dispatch.Fallback, Name: dispatch.Python, Args: []string{`a\b`}, Prog: front, Text: "",
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			if got := dispatch.PluginName(test.args); got != test.candidate {
				t.Errorf("PluginName(%q) = %q, want %q", test.args, got, test.candidate)
			}

			got := dispatch.Decide(test.args, test.available, "dev")
			if !reflect.DeepEqual(got, test.want) {
				t.Errorf("Decide(%q, %q) = %+v", test.args, test.available, got)
				t.Logf("want %+v", test.want)
			}
		})
	}
}

func TestBuiltins(t *testing.T) {
	t.Parallel()

	const (
		help    = "Usage: tbhbdev <command> [args...]\n       tbhbdev --version\n"
		version = "tbhbdev stamped\n"
	)
	for _, test := range []struct {
		name string
		args []string
		text string
	}{
		{"no arguments", nil, help},
		{"help", []string{"--help"}, help},
		{"short help", []string{"-h"}, help},
		{"version", []string{"--version"}, version},
		{"short version", []string{"-V"}, version},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			if got := dispatch.PluginName(test.args); got != "" {
				t.Errorf("PluginName(%q) = %q, want no candidate", test.args, got)
			}

			got := dispatch.Decide(test.args, "", "stamped")
			if got.Action != dispatch.Print || got.Text != test.text {
				t.Errorf("Decide(%q) = %+v, want to print %q", test.args, got, test.text)
			}
		})
	}
}
