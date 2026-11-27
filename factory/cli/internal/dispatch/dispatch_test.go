// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package dispatch_test

import (
	"testing"

	"github.com/tbhb-dev/tbhbdev/factory/cli/internal/dispatch"
)

func TestBuiltinText(t *testing.T) {
	t.Parallel()

	const (
		help    = "Usage: tbhbdev <command> [args...]\n       tbhbdev --version\n"
		version = "tbhbdev stamped\n"
	)

	for _, test := range []struct {
		name string
		want string
		args []string
	}{
		{"no arguments", help, nil},
		{"help", help, []string{"--help"}},
		{"short help", help, []string{"-h"}},
		{"version", version, []string{"--version"}},
		{"short version", version, []string{"-V"}},
		{"unknown command", "", []string{"other", "--help"}},
		{"empty argument", "", []string{""}},
		{"unknown flag", "", []string{"--repo"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			t.Parallel()

			if got := dispatch.BuiltinText(test.args, "stamped"); got != test.want {
				t.Errorf("BuiltinText(%q) = %q, want %q", test.args, got, test.want)
			}
		})
	}
}
