# SPDX-License-Identifier: Apache-2.0
# Copyright Tony Burns

"""Pin the Prometheus rule tester used by the capacity suite."""

_VERSION = "3.11.3"
_SHA256 = {
    "darwin-amd64": "ebd86b50e816e3752edff25202593fc31e08771f14c5f6d4a24737f0304385be",
    "darwin-arm64": "742773c5b3958eec5e6b58802f25cf77b47a319219ce0d508ed2f657c61d8859",
    "linux-amd64": "9479af67673316278958cda1f39b88a09f8921084e039c65acca060d0447bb38",
    "linux-arm64": "d2ec0a96259afde955ad1560ced303cef99cac4dac676bd4dd7614d76adb708a",
}

def _promtool_test_tool_impl(repository_ctx):
    os_name = {"mac os x": "darwin"}.get(repository_ctx.os.name, repository_ctx.os.name)
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(repository_ctx.os.arch, repository_ctx.os.arch)
    platform = os_name + "-" + arch
    if platform not in _SHA256:
        fail("No pinned promtool for " + platform)
    archive = "prometheus-{}.{}".format(_VERSION, platform)
    repository_ctx.download_and_extract(
        url = "https://github.com/prometheus/prometheus/releases/download/v{}/{}.tar.gz".format(_VERSION, archive),
        sha256 = _SHA256[platform],
        stripPrefix = archive,
    )
    repository_ctx.file("BUILD.bazel", 'exports_files(["promtool"])\n')

promtool_test_tool = repository_rule(implementation = _promtool_test_tool_impl)
