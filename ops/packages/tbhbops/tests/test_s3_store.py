# cspell:ignore EXAMPLEADMINACCESS EXAMPLEADMINSECRET EXAMPLEACCESSKEY
# cspell:ignore EXAMPLESECRETKEY EXAMPLEOTHER EXAMPLEOTHERSECRET EXAMPLESECRET
import dataclasses

import pytest

from tbhbops.s3_store import (
    O11Y_BUCKETS,
    Consumer,
    Keys,
    build_identities,
    validate_bucket,
)

ADMIN = Keys("EXAMPLEADMINACCESS0001", "EXAMPLEADMINSECRET0001")
CLIENT = Keys("EXAMPLEACCESSKEY0001", "EXAMPLESECRETKEY0001")
O11Y = Consumer("o11y", O11Y_BUCKETS, CLIENT)


def test_scoped_document_and_no_mutation() -> None:
    other = Consumer("other", ("other-bucket",), Keys("EXAMPLEOTHER", "EXAMPLESECRET"))
    before = dataclasses.asdict(O11Y)
    assert O11Y_BUCKETS == ("loki", "mimir-blocks", "mimir-ruler", "tempo")
    assert build_identities(ADMIN, (O11Y, other)) == {
        "identities": [
            {
                "name": "admin",
                "credentials": [
                    {
                        "accessKey": "EXAMPLEADMINACCESS0001",
                        "secretKey": "EXAMPLEADMINSECRET0001",
                    }
                ],
                "actions": ["Admin"],
            },
            {
                "name": "o11y",
                "credentials": [
                    {
                        "accessKey": "EXAMPLEACCESSKEY0001",
                        "secretKey": "EXAMPLESECRETKEY0001",
                    }
                ],
                "actions": [
                    "Read:loki",
                    "Write:loki",
                    "List:loki",
                    "Read:mimir-blocks",
                    "Write:mimir-blocks",
                    "List:mimir-blocks",
                    "Read:mimir-ruler",
                    "Write:mimir-ruler",
                    "List:mimir-ruler",
                    "Read:tempo",
                    "Write:tempo",
                    "List:tempo",
                ],
            },
            {
                "name": "other",
                "credentials": [
                    {"accessKey": "EXAMPLEOTHER", "secretKey": "EXAMPLESECRET"}
                ],
                "actions": [
                    "Read:other-bucket",
                    "Write:other-bucket",
                    "List:other-bucket",
                ],
            },
        ]
    }
    assert dataclasses.asdict(O11Y) == before
    assert build_identities(ADMIN, ())["identities"][0]["actions"] == ["Admin"]
    assert repr(CLIENT) == "Keys()"
    assert "EXAMPLE" not in repr(O11Y)


@pytest.mark.parametrize("name", ["abc", "a" * 63, "0a0", "a.b", "a-b", "12.34.56"])
def test_valid_buckets(name: str) -> None:
    assert validate_bucket(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        "ab",
        "a" * 64,
        "Aaa",
        "a_a",
        "-abc",
        "abc-",
        ".abc",
        "abc.",
        "a..b",
        "a.-b",
        "a-.b",
        ".".join(["123"] * 4),
        "abc\n",
        "a:b",
        "*",
        "xn--bucket",
        "sthree-bucket",
        "amzn-s3-demo-bucket",
        "bucket-s3alias",
        "bucket--ol-s3",
        "bucket.mrap",
        "bucket--x-s3",
        "bucket--table-s3",
    ],
)
def test_invalid_buckets(name: str) -> None:
    with pytest.raises(ValueError, match=r"^invalid S3 bucket name$"):
        validate_bucket(name)


@pytest.mark.parametrize("name", ["", " ", "admin", "anonymous", "o11y"])
def test_consumer_names(name: str) -> None:
    duplicate = dataclasses.replace(O11Y, name=name)
    with pytest.raises(
        ValueError, match=r"^consumer names must be nonempty, unique and unreserved$"
    ):
        build_identities(ADMIN, (O11Y, duplicate))


@pytest.mark.parametrize("buckets", [(), ("loki", "loki")])
def test_consumer_bucket_list(buckets: tuple[str, ...]) -> None:
    with pytest.raises(
        ValueError, match=r"^each consumer needs a nonempty list of unique buckets$"
    ):
        build_identities(ADMIN, (dataclasses.replace(O11Y, buckets=buckets),))


def test_invalid_consumer_bucket() -> None:
    with pytest.raises(ValueError, match="invalid S3 bucket name"):
        build_identities(ADMIN, (dataclasses.replace(O11Y, buckets=("*",)),))


@pytest.mark.parametrize("value", ["", " ", "\n\t"])
@pytest.mark.parametrize("field", ["access_key", "secret_key"])
@pytest.mark.parametrize("identity", ["admin", "consumer"])
def test_empty_keys(value: str, field: str, identity: str) -> None:
    keys = dataclasses.replace(CLIENT, **{field: value})
    admin, consumer = (
        (keys, O11Y)
        if identity == "admin"
        else (ADMIN, dataclasses.replace(O11Y, keys=keys))
    )
    with pytest.raises(
        ValueError, match=r"^S3 access and secret keys must be nonempty$"
    ):
        build_identities(admin, (consumer,))


def test_reused_keys() -> None:
    with pytest.raises(
        ValueError, match=r"^access keys must be unique across identities$"
    ):
        build_identities(CLIENT, (O11Y,))
    with pytest.raises(ValueError, match="access keys must be unique"):
        build_identities(ADMIN, (O11Y, dataclasses.replace(O11Y, name="other")))
