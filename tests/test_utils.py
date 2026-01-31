import types

import pytest

import main


def test_normalize_text():
    assert main._normalize_text(" Hey,  Sagar!! ") == "hey sagar"


def test_is_exit_command():
    assert main.is_exit_command("please exit now") is True
    assert main.is_exit_command("keep going") is False


def test_is_wake_word_exact():
    assert main.is_wake_word("hey sagar") is True


def test_is_wake_word_contains():
    assert main.is_wake_word("hey sagar please") is True


def test_is_goodbye():
    assert main._is_goodbye("bye") is True
    assert main._is_goodbye("see you") is False


def test_process_command_play_no_song(monkeypatch):
    spoken = {}

    def fake_speak(text, rate=150):
        spoken["text"] = text

    def fake_open(_url):
        raise AssertionError("_open_url should not be called")

    monkeypatch.setattr(main, "speak", fake_speak)
    monkeypatch.setattr(main, "_open_url", fake_open)

    _ctx, exit_to_wake = main.prossesCommand("play", context=None)
    assert exit_to_wake is False
    assert "Please say the song name" in spoken["text"]


def test_process_command_open_google(monkeypatch):
    opened = {}

    def fake_open(url):
        opened["url"] = url

    monkeypatch.setattr(main, "_open_url", fake_open)
    monkeypatch.setattr(main, "speak", lambda *_args, **_kwargs: None)

    _ctx, exit_to_wake = main.prossesCommand("open google", context=None)
    assert exit_to_wake is False
    assert opened["url"] == "www.google.com"
