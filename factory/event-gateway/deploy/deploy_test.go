// SPDX-License-Identifier: Apache-2.0
// Copyright Tony Burns

package deploy_test

// cspell:words tailnet

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"slices"
	"strings"
	"testing"
)

const (
	digest        = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	repo          = "registry.example/event-gateway"
	pull          = "--pull"
	never         = "never"
	hook          = "github_webhook_secret"
	sessionTokens = "event_gateway_session_tokens"
	token         = "tunnel_token"
	bindAddr      = "100.64.0.1"
	bindEnv       = "EVENT_GATEWAY_SESSION_BIND=" + bindAddr
	secretUID     = 65532
)

// result is what one run of up.sh left behind.
type result struct {
	output string
	record string
	dir    string
	code   int
}

func must(t *testing.T, err error) {
	t.Helper()

	if err != nil {
		t.Fatal(err)
	}
}

// fixture builds a private copy of up.sh beside secrets with mode 400. PATH
// lists only the stub directory and links to host tools. The docker stub
// records the image from its environment and its arguments. The find stub
// drops the owner test, which a test account can't satisfy; a test can swap
// in the real find to see the owner refused.
func fixture(t *testing.T) string {
	t.Helper()

	dir := t.TempDir()
	root, err := os.OpenRoot(dir)
	must(t, err)
	t.Cleanup(func() { must(t, root.Close()) })

	write := func(name, content string, mode os.FileMode) {
		must(t, root.MkdirAll(filepath.Dir(name), 0o700))
		must(t, root.WriteFile(name, []byte(content), mode))
	}

	script, err := os.ReadFile("up.sh")
	must(t, err)

	realFind, err := exec.LookPath("find")
	must(t, err)

	write("up.sh", string(script), 0o600)
	write("secrets/"+hook, "", 0o400)
	write("secrets/"+sessionTokens, "", 0o400)
	write("secrets/"+token, "", 0o400)
	write(
		"docker",
		"#!/bin/sh\ncd \"${0%/*}\" || exit 1\n"+
			"printf '%s\\0' \"${EVENT_GATEWAY_IMAGE:-}\" \"${EVENT_GATEWAY_SESSION_BIND:-}\" \"$@\" > record\n",
		0o700,
	)
	write("sudo", "#!/bin/sh\nexec env -i PATH=\"$PATH\" \"$@\"\n", 0o700)
	write("find", "#!/bin/sh\nexec "+realFind+" \"$1\" -type f -perm 0400\n", 0o700)

	for _, tool := range []string{"grep", "dirname", "env"} {
		path, lookErr := exec.LookPath(tool)
		must(t, lookErr)

		must(t, os.Symlink(path, filepath.Join(dir, tool)))
	}

	return dir
}

// runUp runs up.sh in a fixture that mutate may change first.
func runUp(t *testing.T, mutate func(dir string), env []string, args ...string) result {
	t.Helper()

	if runtime.GOOS == "windows" {
		t.Skip("up.sh needs a POSIX shell")
	}

	dir := fixture(t)
	if mutate != nil {
		mutate(dir)
	}

	cmd := exec.CommandContext(t.Context(), "/bin/sh", "up.sh")
	cmd.Dir = dir
	cmd.Args = append(cmd.Args, args...)

	cmd.Env = append([]string{"PATH=" + dir}, env...)

	out, err := cmd.CombinedOutput()
	res := result{output: string(out), record: "", dir: dir, code: 0}

	if exitErr, ok := errors.AsType[*exec.ExitError](err); ok {
		res.code = exitErr.ExitCode()
	}

	// The record is absent when up.sh never reached docker.
	root, openErr := os.OpenRoot(dir)

	must(t, openErr)
	defer func() { must(t, root.Close()) }()

	got, err := root.ReadFile("record")
	if !errors.Is(err, os.ErrNotExist) {
		must(t, err)

		res.record = string(got)
	}

	return res
}

// checkRan expects up.sh to hand docker the image, bind address and Compose arguments.
func checkRan(t *testing.T, env []string, image string, args ...string) {
	t.Helper()

	res := runUp(t, nil, append([]string{bindEnv}, env...), append([]string{image}, args...)...)
	want := append([]string{
		image, bindAddr, "compose", "-f", filepath.Join(res.dir, "compose.yaml"), "up", "-d", "--force-recreate",
	}, args...)

	if got := strings.Split(strings.TrimSuffix(res.record, "\x00"), "\x00"); res.code != 0 || !slices.Equal(got, want) {
		t.Errorf("up.sh %s: exit %d, docker saw %q, want %q: %s", image, res.code, got, want, res.output)
	}
}

func TestUpAccepts(t *testing.T) {
	t.Parallel()

	checkRan(t, nil, repo+"@"+digest)
	checkRan(t, nil, digest, pull, never)
	checkRan(t, []string{"DEPLOY_DOCKER_SUDO=1"}, repo+"@"+digest)
}

// checkRefused expects up.sh to exit 2 before it starts Compose.
func checkRefused(t *testing.T, mutate func(string), contains string, args ...string) {
	t.Helper()

	checkEnvRefused(t, mutate, []string{bindEnv}, contains, args...)
}

func checkEnvRefused(t *testing.T, mutate func(string), env []string, contains string, args ...string) {
	t.Helper()

	res := runUp(t, mutate, env, args...)
	if res.code != 2 || res.record != "" || !strings.Contains(res.output, contains) {
		t.Errorf("up.sh %q (env %q): exit %d, docker saw %q, want exit 2 and no run: %s",
			args, env, res.code, res.record, res.output)
	}
}

func TestUpRefusesOtherArguments(t *testing.T) {
	t.Parallel()

	for name, args := range map[string][]string{
		"pull always":      {pull, "always"},
		"extra after pull": {pull, never, "--force-recreate"},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			checkRefused(t, nil, "only argument allowed", append([]string{repo + "@" + digest}, args...)...)
		})
	}
}

func TestUpRefusesOtherReferences(t *testing.T) {
	t.Parallel()

	for name, image := range map[string]string{
		"tag":            repo + ":sha-abc",
		"latest":         repo + ":latest",
		"short digest":   repo + "@sha256:abc",
		"upper digest":   repo + "@sha256:" + strings.Repeat("A", 64),
		"id no prefix":   strings.TrimPrefix(digest, "sha256:"),
		"tag and digest": repo + ":v1@" + digest,
		"no name":        "@" + digest,
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			checkRefused(t, nil, "up.sh: the image must", image)
		})
	}
}

func fileMode(t *testing.T, path func(string) string, mode os.FileMode) func(string) {
	t.Helper()

	return func(dir string) { must(t, os.Chmod(path(dir), mode)) }
}

func TestUpRequiresSecrets(t *testing.T) {
	t.Parallel()

	for _, name := range []string{hook, sessionTokens, token} {
		path := func(dir string) string { return filepath.Join(dir, "secrets", name) }

		for kind, mutate := range map[string]func(string){
			"mode 440": fileMode(t, path, 0o440),
			"mode 600": fileMode(t, path, 0o600),
			"symlink": func(dir string) {
				must(t, os.Remove(path(dir)))
				must(t, os.Symlink(filepath.Join(dir, "up.sh"), path(dir)))
			},
			"directory": func(dir string) {
				must(t, os.Remove(path(dir)))
				must(t, os.Mkdir(path(dir), 0o700))
				must(t, os.WriteFile(filepath.Join(path(dir), "child"), nil, 0o400))
			},
			"missing": func(dir string) { must(t, os.Remove(path(dir))) },
		} {
			t.Run(name+" "+kind, func(t *testing.T) {
				t.Parallel()

				checkRefused(t, mutate, "secrets/"+name+" must be", digest)
			})
		}
	}
}

func TestUpRequiresSessionBind(t *testing.T) {
	t.Parallel()

	checkEnvRefused(t, nil, nil, "EVENT_GATEWAY_SESSION_BIND must be", digest)

	for name, bind := range map[string]string{
		"empty":          "",
		"all interfaces": "0.0.0.0",
		"zero network":   "0.1.2.3",
		"ipv6":           "::",
		"bracketed ipv6": "[::]",
		"hostname":       "localhost",
		"whitespace":     "100.64.0.1 100.64.0.2",
		"newline":        "100.64.0.1\n100.64.0.2",
		"port":           "1.2.3.4:5",
		"cidr":           "100.64.0.1/32",
		"octet range":    "256.0.0.1",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()

			env := []string{"EVENT_GATEWAY_SESSION_BIND=" + bind}
			checkEnvRefused(t, nil, env, "EVENT_GATEWAY_SESSION_BIND must be", digest)
		})
	}
}

// TestUpRequiresOwner swaps in the real find, which sees another owner.
func TestUpRequiresOwner(t *testing.T) {
	t.Parallel()

	if os.Getuid() == secretUID {
		t.Skip("fixture files already belong to uid 65532")
	}

	checkRefused(t, func(dir string) {
		realFind, err := exec.LookPath("find")
		must(t, err)
		must(t, os.Remove(filepath.Join(dir, "find")))
		must(t, os.Symlink(realFind, filepath.Join(dir, "find")))
	}, "secrets/"+hook+" must be", digest)
}

func composeText(t *testing.T) string {
	t.Helper()

	raw, err := os.ReadFile("compose.yaml")
	must(t, err)

	return string(raw)
}

// serviceBlock returns the lines of one service, comments included.
func serviceBlock(t *testing.T, service string) string {
	t.Helper()

	var block []string

	inside := false

	for line := range strings.SplitSeq(composeText(t), "\n") {
		switch {
		case line == "  "+service+":":
			inside = true
		case line != "" && !strings.HasPrefix(line, "   ") && !strings.HasPrefix(strings.TrimSpace(line), "#"):
			inside = false
		}

		if inside {
			block = append(block, line)
		}
	}

	if len(block) == 0 {
		t.Fatalf("compose.yaml has no %s service", service)
	}

	return strings.Join(block, "\n")
}

func TestComposeSettings(t *testing.T) {
	t.Parallel()

	whole := composeText(t)
	gateway := serviceBlock(t, "gateway")
	tunnel := serviceBlock(t, "tunnel")
	hardening := []string{
		"\n    user: \"65532:65532\"\n",
		"\n    read_only: true\n",
		"\n    cap_drop: [ALL]\n",
		"\n    security_opt:\n      - no-new-privileges:true\n",
		"\n    restart: unless-stopped\n",
		"\n    networks:\n      - edge\n",
		"\n      driver: local\n",
		"\n        max-size: \"10m\"\n        max-file: \"3\"\n",
	}

	for block, wants := range map[string][]string{
		gateway: append([]string{
			"\n    image: ${EVENT_GATEWAY_IMAGE:?",
			"\n    command:\n      - serve\n      - -listen\n      - \":8080\"\n      - -session-listen\n" +
				"      - \":8081\"\n      - -read-token-file\n      - /run/secrets/event_gateway_session_tokens\n" +
				"      - -github-secret-file\n      - /run/secrets/github_webhook_secret\n" +
				"      - -store-dir\n      - /data/nats\n",
			"\n    ports:\n      # yamllint disable-line rule:line-length\n      - \"" +
				"${EVENT_GATEWAY_SESSION_BIND:?set the host address for the session port, for example a tailnet address}" +
				":8081:8081\"\n    volumes:\n      - data:/data\n",
			"\n    secrets:\n      - source: github_webhook_secret\n        uid: \"65532\"\n" +
				"        gid: \"65532\"\n        mode: 0400\n      - source: event_gateway_session_tokens\n" +
				"        uid: \"65532\"\n        gid: \"65532\"\n        mode: 0400\n",
			"\n      test: [\"CMD\", \"/event-gateway\", \"healthcheck\"]\n      interval: 30s\n" +
				"      timeout: 5s\n      retries: 3\n      start_period: 5s\n",
		}, hardening...),
		tunnel: append([]string{
			"\n    runtime: runc\n", "\n    secrets:\n      - tunnel_token\n",
			"\n      - --token-file\n      - /run/secrets/tunnel_token\n",
			"\n    depends_on:\n      gateway:\n        condition: service_healthy\n",
		}, hardening...),
	} {
		for _, want := range wants {
			if !strings.Contains(block, want) {
				t.Errorf("%s lacks %q", strings.SplitN(block, "\n", 2)[0], strings.TrimSpace(want))
			}
		}
	}

	checkComposeIsolation(t, whole, tunnel)
}

func checkComposeIsolation(t *testing.T, whole, tunnel string) {
	t.Helper()

	image := regexp.MustCompile(`(?m)^    image: cloudflare/cloudflared:[0-9][0-9.]*@sha256:[0-9a-f]{64}$`)
	if !image.MatchString(tunnel) {
		t.Error("the tunnel image is not cloudflare/cloudflared:<tag>@sha256:<64 hex digits>")
	}

	for _, want := range []string{
		"\nname: event-gateway\n", "\nvolumes:\n  data: {}\n",
		"\n  github_webhook_secret:\n    file: ./secrets/github_webhook_secret\n",
		"\n  event_gateway_session_tokens:\n    file: ./secrets/event_gateway_session_tokens\n",
		"\n  tunnel_token:\n    file: ./secrets/tunnel_token\n",
	} {
		if !strings.Contains(whole, want) {
			t.Errorf("compose.yaml lacks %q", strings.TrimSpace(want))
		}
	}

	if strings.Count(whole, "event_gateway_session_tokens:") != 1 ||
		strings.Count(whole, "ports:") != 1 ||
		strings.Contains(tunnel, sessionTokens) || strings.Contains(tunnel, "ports:") {
		t.Error("session tokens or ports leaked outside gateway")
	}

	for _, banned := range []string{
		":8080:", "8080:8080", "EVENT_GATEWAY_SESSION_BIND:-", "EVENT_GATEWAY_SESSION_BIND-",
		"network_mode", "TUNNEL_TOKEN", "-grafana-secret-file", "-grafana-secret",
		"-read-listen", "labels:", "json-file",
	} {
		if strings.Contains(whole, banned) {
			t.Errorf("compose.yaml contains %q", banned)
		}
	}
}
