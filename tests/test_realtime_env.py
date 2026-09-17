import pytest

from mm_agents.realtime_env import describe_sources, load_env_file, parse_env_text


def test_parse_handles_comments_quotes_and_export(tmp_path):
    text = (
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        "export EXPORTED=other\n"
        "SPACED = padded \n"
        'DOUBLE="two words"\n'
        "SINGLE='kept # hash'\n"
        "TRAILING=kept # dropped\n"
        "ESCAPED=\"line\\nbreak\"\n"
    )
    assert parse_env_text(text) == {
        "PLAIN": "value",
        "EXPORTED": "other",
        "SPACED": "padded",
        "DOUBLE": "two words",
        "SINGLE": "kept # hash",
        "TRAILING": "kept",
        "ESCAPED": "line\nbreak",
    }


@pytest.mark.parametrize("line", ["NOEQUALS", "=novalue", "1BAD=x", "BAD-NAME=x"])
def test_parse_rejects_malformed_lines(line):
    with pytest.raises(ValueError):
        parse_env_text(line + "\n")


def test_load_env_file_keeps_the_shell_and_skips_blanks(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=from-file\nEMPTY=\nADD=from-file\n", encoding="utf-8")
    environ = {"KEEP": "from-shell"}

    applied = load_env_file(env_file, environ=environ)

    assert applied == {"ADD": "from-file"}
    assert environ == {"KEEP": "from-shell", "ADD": "from-file"}


def test_load_env_file_override_is_explicit(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("KEEP=from-file\n", encoding="utf-8")
    environ = {"KEEP": "from-shell"}

    assert load_env_file(env_file, override=True, environ=environ) == {"KEEP": "from-file"}
    assert environ["KEEP"] == "from-file"


def test_missing_file_is_not_an_error(tmp_path):
    assert load_env_file(tmp_path / "absent", environ={}) == {}


def test_describe_sources_never_prints_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET_TOKEN=sk-do-not-print\n", encoding="utf-8")
    environ = {}
    applied = load_env_file(env_file, environ=environ)

    described = describe_sources(applied, env_file)

    assert "SECRET_TOKEN" in described
    assert "sk-do-not-print" not in described
    assert "no values loaded" in describe_sources({}, env_file)
