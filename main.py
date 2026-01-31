# main.py
# Dependencies:
#   pip install pyaudio SpeechRecognition groq pyttsx3
#
# Usage:
#   set GROQ_API_KEY in your environment if you want Groq responses:
#     export GROQ_API_KEY="your_key"    (Linux / macOS)
#     setx GROQ_API_KEY "your_key"      (Windows - persistent)
#   python main.py

import os
import time
import subprocess
import webbrowser
import logging
import shutil
from dataclasses import dataclass
from urllib.parse import quote
import speech_recognition as sr
import pyttsx3

# Optional imports (if present in your project)
try:
    import contentLinks  # custom module for mapping song names to links (optional)
except Exception:
    contentLinks = None

# Groq (optional); we'll only use it if GROQ_API_KEY is set
try:
    from groq import Groq
except Exception:
    Groq = None

# ---------- Configuration ----------
@dataclass(frozen=True)
class Config:
    use_powershell_tts: bool
    groq_api_key: str
    groq_model: str
    listen_timeout: int
    phrase_time_limit: int
    wakeword_timeout: int
    wakeword_phrase_limit: float
    wakeword_energy_threshold: int
    ambient_adjust_duration: float
    wakeword_recalibrate_every: int
    wakeword_failure_recalibrate_after: int
    failure_recalibrate_after: int
    vscode_path: str | None
    min_command_words: int
    followup_timeout: int
    followup_phrase_limit: int

    @staticmethod
    def from_env() -> "Config":
        return Config(
            use_powershell_tts=os.name == "nt",
            groq_api_key=os.getenv("GROQ_API_KEY", "").strip(),
            groq_model=os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
            listen_timeout=int(os.getenv("LISTEN_TIMEOUT", "25")),
            phrase_time_limit=int(os.getenv("PHRASE_TIME_LIMIT", "20")),
            wakeword_timeout=int(os.getenv("WAKEWORD_TIMEOUT", "8")),
            wakeword_phrase_limit=float(os.getenv("WAKEWORD_PHRASE_LIMIT", "4.0")),
            wakeword_energy_threshold=int(os.getenv("WAKEWORD_ENERGY_THRESHOLD", "250")),
            ambient_adjust_duration=float(os.getenv("AMBIENT_ADJUST_DURATION", "0.4")),
            wakeword_recalibrate_every=int(os.getenv("WAKEWORD_RECALIBRATE_EVERY", "0")),
            wakeword_failure_recalibrate_after=int(os.getenv("WAKEWORD_FAILURE_RECALIBRATE_AFTER", "1")),
            failure_recalibrate_after=int(os.getenv("FAILURE_RECALIBRATE_AFTER", "2")),
            vscode_path=os.getenv("VSCODE_PATH"),
            min_command_words=int(os.getenv("MIN_COMMAND_WORDS", "4")),
            followup_timeout=int(os.getenv("FOLLOWUP_TIMEOUT", "8")),
            followup_phrase_limit=int(os.getenv("FOLLOWUP_PHRASE_LIMIT", "12")),
        )


CONFIG = Config.from_env()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("assistant")

# ---------- TTS Setup ----------
engine = None
if not CONFIG.use_powershell_tts:
    try:
        engine = pyttsx3.init()
        # try to set a voice safely
        voices = engine.getProperty("voices")
        if voices:
            engine.setProperty("voice", voices[0].id)
        engine.setProperty("volume", 1.0)
    except Exception:
        engine = None


def _escape_powershell_string(text: str) -> str:
    return text.replace("'", "''")


def _speak_powershell(txt: str, rate: int = 0) -> None:
    safe_text = _escape_powershell_string(str(txt))
    command = (
        "Add-Type -AssemblyName System.Speech;"
        "$speak = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$speak.Rate = {rate};"
        f"$speak.Speak('{safe_text}');"
    )
    # run PowerShell quietly
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def speak(txt: str, rate: int = 150) -> None:
    """
    Convert text to speech. Uses PowerShell on Windows (reliable)
    or pyttsx3 on other platforms if available.
    """
    if not txt:
        return
    if CONFIG.use_powershell_tts:
        _speak_powershell(str(txt).strip(), rate)
        return

    if engine is None:
        # If TTS engine missing, fallback: print message only
        print("[TTS unavailable] ", txt)
        return

    try:
        engine.setProperty("rate", rate)
        engine.say(str(txt).strip())
        engine.runAndWait()
    except Exception as e:
        print("TTS error:", e)


def _configure_recognizer(recognizer: sr.Recognizer, mode: str = "wake") -> None:
    """
    Configure recognizer settings for more reliable listening.
    """
    recognizer.dynamic_energy_threshold = True
    # Wake word needs faster endpointing; active mode can be slightly more patient
    if mode == "wake":
        recognizer.pause_threshold = 0.9
        recognizer.non_speaking_duration = 0.25
        recognizer.phrase_threshold = 0.3
        recognizer.energy_threshold = CONFIG.wakeword_energy_threshold
        recognizer.dynamic_energy_adjustment_damping = 0.2
        recognizer.dynamic_energy_ratio = 1.2
    else:
        # Be more patient in active mode to avoid cutting users off mid-sentence
        recognizer.pause_threshold = 2.2
        recognizer.non_speaking_duration = 0.8
        recognizer.phrase_threshold = 0.6
        recognizer.dynamic_energy_adjustment_damping = 0.15
        recognizer.dynamic_energy_ratio = 1.6


# ---------- Utilities ----------
GOODBYE_TERMS = {"good bye", "goodbye", "bye", "exit", "quit", "stop"}

LAST_PROVIDED_URL = None


def _is_goodbye(text: str) -> bool:
    """
    Returns True if the text contains any goodbye intent word.
    Handles small punctuation and multi-word greetings.
    """
    if not text:
        return False
    text = text.lower().strip()
    if text in GOODBYE_TERMS:
        return True
    words = {w.strip(".,!?") for w in text.split() if w.strip(".,!?")}
    return any(term in words for term in GOODBYE_TERMS)


def _extract_first_url(text: str) -> str | None:
    if not text:
        return None
    try:
        import re

        match = re.search(r"(https?://[^\s]+)", text)
        if match:
            return match.group(1).rstrip(".,)")
        match = re.search(r"(www\.[^\s]+)", text)
        if match:
            return "https://" + match.group(1).rstrip(".,)")
    except Exception:
        return None
    return None


# ---------- AI integration (optional) ----------
_groq_client = None
if Groq and CONFIG.groq_api_key:
    try:
        _groq_client = Groq(api_key=CONFIG.groq_api_key)
    except Exception:
        _groq_client = None
else:
    _groq_client = None


def aiProcess(command: str, context=None) -> str:
    """
    Generate a response using Groq API if available.
    If not available, returns a simple fallback string.
    """
    if not command:
        return "I didn't hear anything."

    if _groq_client is None:
        # graceful fallback when no API access
        # Keep replies short and assistant-like (good for voice)
        return f"Sorry, AI access is not configured. You said: {command}"

    # Build messages
    messages = [
        {
            "role": "system",
            "content": (
                "You are a concise voice assistant named Sagar Biswas. "
                "Give short, clear replies suitable for speech output."
            ),
        }
    ]
    if context:
        messages.extend(context)

    messages.append({"role": "user", "content": command})

    try:
        completion = _groq_client.chat.completions.create(
            model=CONFIG.groq_model,
            messages=messages,
        )
        # The Groq SDK returns choices; robustly access them
        choice = getattr(completion, "choices", None)
        if choice and len(choice) > 0:
            # support differing structures defensively
            msg = choice[0].message.content if hasattr(choice[0], "message") else choice[0].get("message", {}).get("content", "")
            if not msg:
                # fallback to raw text if present
                msg = getattr(choice[0], "text", "") or str(choice[0])
            return str(msg)
        # fallback
        return str(completion)
    except Exception as e:
        print("Error calling Groq API:", e)
        return f"Sorry, I couldn't reach the AI service. You said: {command}"


# ---------- Command processing ----------
def _open_url(url: str) -> None:
    """Helper to open a URL in a new browser tab; ensures scheme exists."""
    if not url:
        return
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    global LAST_PROVIDED_URL
    LAST_PROVIDED_URL = url
    try:
        webbrowser.open_new_tab(url)
    except Exception as e:
        logger.error("Could not open browser: %s", e)


def _recognize_google_any(recognizer: sr.Recognizer, audio) -> list:
    """
    Return a list of possible transcripts from Google Speech.
    """
    try:
        result = recognizer.recognize_google(audio, show_all=True)
    except sr.RequestError:
        raise
    except Exception:
        return []

    if isinstance(result, str):
        return [result]
    if isinstance(result, dict):
        alts = result.get("alternative") or []
        return [a.get("transcript", "") for a in alts if a.get("transcript")]
    return []


def _strip_leading_fillers(text: str) -> str:
    if not text:
        return ""
    words = text.strip().split()
    fillers = {"for", "the", "a", "an", "to", "please", "your", "this", "that"}
    while words and words[0].lower() in fillers:
        words.pop(0)
    return " ".join(words)


def _is_link_reference(text: str) -> bool:
    if not text:
        return False
    norm = _normalize_text(text)
    if not norm:
        return False
    phrases = {
        "provided link",
        "your provided link",
        "the provided link",
        "that link",
        "the link",
        "this link",
        "provided url",
        "your provided url",
        "the provided url",
        "that url",
        "the url",
        "this url",
        "link",
        "url",
    }
    if any(p in norm for p in phrases):
        return True
    words = norm.split()
    if "link" in words or "url" in words:
        return len(words) <= 3
    return False

def _resolve_vscode_path() -> str | None:
    if CONFIG.vscode_path and os.path.exists(CONFIG.vscode_path):
        return CONFIG.vscode_path
    code_path = shutil.which("code")
    if code_path:
        return code_path
    default_path = r"C:\Users\%USERNAME%\AppData\Local\Programs\Microsoft VS Code\Code.exe"
    expanded = os.path.expandvars(default_path)
    if os.path.exists(expanded):
        return expanded
    return None


def open_vscode():
    """
    Open Visual Studio Code using an absolute path (Windows).
    """
    vscode_path = _resolve_vscode_path()

    if not vscode_path:
        speak("Visual Studio Code is not installed in the expected location.")
        return

    try:
        subprocess.Popen([vscode_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        logger.error("Error opening VS Code: %s", e)
        speak("Sorry, I couldn't open Visual Studio Code.")

def close_vscode():
    """
    Close all running Visual Studio Code windows (Windows).
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "Code.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False
        )
        speak("Visual Studio Code closed.")
    except Exception as e:
        logger.error("Error closing VS Code: %s", e)
        speak("Sorry, I couldn't close Visual Studio Code.")



def prossesCommand(c: str, context=None):
    """
    Process a single spoken command.
    Returns (context, exit_to_wake) where exit_to_wake=True means user said goodbye.
    """

    global LAST_PROVIDED_URL   # ✅ MUST be first

    if not c:
        return context, False

    command = c.strip()
    lcmd = command.lower()

    if lcmd in {"open vscode", "open vs code", "open visual studio code"}:
        open_vscode()
        speak("Opening Visual Studio Code.")
        return context, False

    if lcmd in {"close vscode", "close vs code", "close visual studio code"}:
        close_vscode()
        return context, False

    # Quick built-ins (exact matches are checked first for speed)
    if lcmd == "open google":
        _open_url("www.google.com")
        return context, False
    if lcmd == "open facebook":
        _open_url("www.facebook.com")
        return context, False
    if lcmd == "open youtube":
        _open_url("www.youtube.com")
        return context, False
    if lcmd == "open github":
        _open_url("www.github.com")
        return context, False
    if lcmd == "open stack overflow" or lcmd == "open stackoverflow":
        _open_url("www.stackoverflow.com")
        return context, False
    if lcmd == "open linkedin":
        _open_url("www.linkedin.com")
        return context, False

    if lcmd.startswith("play"):
        # Support "play bohemian rhapsody" or "play: song name"
        # Use maxsplit=1 to keep the full song name
        try:
            parts = command.split(" ", 1)
            song = parts[1].strip() if len(parts) > 1 else ""
            if not song and ":" in command:
                # fallback if user said "play: song"
                song = command.split(":", 1)[1].strip()
            if not song:
                speak("Please say the song name after 'play'.")
                return context, False

            song = _strip_leading_fillers(song)
            song_norm = _normalize_text(song)

            if _is_link_reference(song_norm):
                if LAST_PROVIDED_URL:
                    _open_url(LAST_PROVIDED_URL)
                    speak("Playing the provided link.")
                else:
                    speak("I don't have a provided link yet.")
                return context, False

            if song.startswith(("http://", "https://", "www.")):
                _open_url(song)
                speak("Playing the link.")
                return context, False

            # Try contentLinks if available (must be a mapping)
            link = None
            if contentLinks and hasattr(contentLinks, "Links"):
                try:
                    link = contentLinks.Links.get(song.lower())
                except Exception:
                    link = None

            if link:
                _open_url(link)
                speak(f"Playing {song}")
            else:
                print(f"I don't have {song} in the library. Opening a web search.")
                speak(f"I don't have {song} in the library. Opening a web search.")
                _open_url("https://www.youtube.com/results?search_query=" + quote(song))
            return context, False
        except Exception as e:
            logger.error("Play command error: %s", e)
            return context, False

    if lcmd.startswith("search"):
        # Support "search today's latest news" or "search: query"
        try:
            parts = command.split(" ", 1)
            query = parts[1].strip() if len(parts) > 1 else ""
            if not query and ":" in command:
                query = command.split(":", 1)[1].strip()
            if not query:
                speak("Please say what you want me to search for.")
                return context, False

            speak(f"Searching for {query}")
            _open_url("https://www.google.com/search?q=" + quote(query))
            return context, False
        except Exception as e:
            logger.error("Search command error: %s", e)
            return context, False

    # Goodbye handling
    if _is_goodbye(command):
        print("\n")
        output = "Goodbye! It was nice assisting you. Say 'hey sagar' when you need me."
        logger.info("AI Response: %s", output)
        speak(output)
        if context is None:
            context = []
        context.append({"role": "user", "content": command})
        context.append({"role": "assistant", "content": output})
        return context, True

    # Otherwise, fallback to AI processing (or fallback string)
    output = str(aiProcess(command, context)).strip()
    logger.info("AI Response: \n%s", output)
    speak(output)

    found_url = _extract_first_url(output)
    if found_url:
        LAST_PROVIDED_URL = found_url

    if context is None:
        context = []

    context.append({"role": "user", "content": command})
    context.append({"role": "assistant", "content": output})

    return context, False


# ---------- Listening loop (active mode) ----------
def listen_and_respond(context=None):
    """
    Active listening mode. Keeps listening and responding until the user says goodbye.
    Returns True only when user said goodbye (so main loop can go back to wake-word).
    """
    recognizer = sr.Recognizer()
    _configure_recognizer(recognizer, mode="active")
    mic = sr.Microphone()
    failures = 0

    while True:
        try:
            with mic as source:
                # Recalibrate only after several consecutive failures
                print("\n")
                if failures >= CONFIG.failure_recalibrate_after:
                    recognizer.adjust_for_ambient_noise(
                        source, duration=CONFIG.ambient_adjust_duration
                    )
                    failures = 0
                logger.info("Sagar listening...")
                audio = recognizer.listen(
                    source,
                    timeout=CONFIG.listen_timeout,
                    phrase_time_limit=CONFIG.phrase_time_limit,
                )
                try:
                    alternatives = _recognize_google_any(recognizer, audio)
                    if not alternatives:
                        raise sr.UnknownValueError()
                    command = max(alternatives, key=lambda s: len(s.split()))
                except sr.UnknownValueError:
                    # Could not parse audio
                    logger.info("Sorry, I didn't catch that.")
                    failures += 1
                    continue
                except sr.RequestError as e:
                    # API/service error (network or service)
                    logger.error("Speech recognition service error: %s", e)
                    speak("Speech service error. Please check your internet connection.")
                    continue

                print("\n")
                failures = 0
                if _needs_followup(command) and not _is_quick_command(command):
                    followup = _listen_followup(recognizer, mic)
                    if followup:
                        command = f"{command} {followup}".strip()
                    if _needs_followup(command):
                        followup2 = _listen_followup(recognizer, mic)
                        if followup2:
                            command = f"{command} {followup2}".strip()
                logger.info("Command: %s", command)
                context, exit_to_wake = prossesCommand(command, context)
                if exit_to_wake:
                    # user said goodbye; return to wake-word mode
                    return True

        except sr.WaitTimeoutError:
            # Nothing said; go back to listening in active mode
            logger.info("Listening timed out (no speech detected).")
            continue
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received. Exiting.")
            speak("Goodbye.\n")
            raise
        except Exception as e:
            logger.error("Unexpected error in active listening: %s", e)
            # keep the assistant alive; don't drop to wake-word mode unexpectedly
            continue


WAKE_WORDS = {
    "hey",
    "hey sagar",
    "sagar",
    "multihat",
    "hello",
}

def _normalize_text(text: str) -> str:
    """
    Normalize text for wake-word matching.
    """
    if not text:
        return ""
    text = text.lower().strip()
    # remove simple punctuation and extra spaces
    text = " ".join([w.strip(".,!?;:") for w in text.split() if w.strip(".,!?;:")])
    return text

EXIT_TERMS = {"exit", "quit", "stop", "close assistant"}

def is_exit_command(text: str) -> bool:
    if not text:
        return False
    text = text.lower().strip()
    words = {w.strip(".,!?") for w in text.split()}
    return any(term in words for term in EXIT_TERMS)


def _fuzzy_match(text: str, target: str, threshold: float = 0.82) -> bool:
    """
    Fuzzy match two strings to tolerate minor recognition errors.
    """
    if not text or not target:
        return False
    try:
        from difflib import SequenceMatcher
        ratio = SequenceMatcher(None, text, target).ratio()
        return ratio >= threshold
    except Exception:
        return False

def is_wake_word(text: str) -> bool:
    """
    Check if recognized speech matches any supported wake word.
    Allows extra words and minor recognition errors.
    """
    norm = _normalize_text(text)
    if not norm:
        return False

    # direct match
    if norm in WAKE_WORDS:
        return True

    # contained phrase match (e.g., "hey sagar please")
    for wake in WAKE_WORDS:
        if wake in norm:
            return True

    # fuzzy match as fallback
    for wake in WAKE_WORDS:
        if _fuzzy_match(norm, wake):
            return True

    return False


def _word_count(text: str) -> int:
    return len([w for w in text.split() if w.strip(".,!?;:")])


def _needs_followup(text: str) -> bool:
    if not text:
        return False
    return _word_count(text) < CONFIG.min_command_words or _ends_with_incomplete_phrase(text)


def _ends_with_incomplete_phrase(text: str) -> bool:
    tail = _normalize_text(text)
    if not tail:
        return False
    if tail.startswith("tell me about") and _word_count(tail) <= 4:
        return True
    if tail.endswith(("youtube", "video", "videos", "link", "links", "learning", "learn")):
        return True
    words = [w for w in tail.split() if w]
    if len(words) >= 2:
        last_word = words[-1]
        prev_word = words[-2]
        if prev_word in {"for", "about", "on", "of", "to", "with", "regarding", "concerning"} and len(last_word) <= 3:
            return True
    for phrase in (
        "tell me about",
        "can you",
        "could you",
        "would you",
        "i want to",
        "i need",
        "i would like",
        "about",
        "on",
        "of",
        "to",
        "for",
        "with",
        "regarding",
        "concerning",
    ):
        if tail.endswith(phrase):
            return True
    return False


def _is_quick_command(text: str) -> bool:
    norm = _normalize_text(text)
    if not norm:
        return False
    if norm in {
        "open google",
        "open facebook",
        "open youtube",
        "open github",
        "open stack overflow",
        "open stackoverflow",
        "open linkedin",
        "open vscode",
        "open vs code",
        "open visual studio code",
        "close vscode",
        "close vs code",
        "close visual studio code",
    }:
        return True
    return norm.startswith("play ") or norm.startswith("search ") or _is_goodbye(norm)


def _listen_followup(recognizer: sr.Recognizer, mic: sr.Microphone) -> str:
    try:
        with mic as source:
            audio = recognizer.listen(
                source,
                timeout=CONFIG.followup_timeout,
                phrase_time_limit=CONFIG.followup_phrase_limit,
            )
        alternatives = _recognize_google_any(recognizer, audio)
        if not alternatives:
            return ""
        return max(alternatives, key=lambda s: len(s.split()))
    except Exception:
        return ""


# ---------- Main (wake-word) loop ----------
def main():
    print("\n")
    context = None
    recognizer = sr.Recognizer()
    _configure_recognizer(recognizer, mode="wake")
    wake_checks = 0
    wake_failures = 0
    mic = sr.Microphone()

    logger.info("Sagar voice assistant starting. Say 'hey sagar' to activate.")
    speak("Sagar voice assistant starting. Say 'hey sagar' to activate.")

    # Initial ambient calibration to reduce false negatives
    try:
        with sr.Microphone() as source:
            logger.info("Calibrating microphone...")
            recognizer.adjust_for_ambient_noise(source, duration=CONFIG.ambient_adjust_duration)
    except Exception as e:
        logger.error("Microphone calibration error: %s", e)

    while True:
        print("\n")
        logger.info("Recognizing...")
        try:
            with mic as source:
                logger.info("Listening...")
                # Recalibrate occasionally or after consecutive failures
                if CONFIG.wakeword_recalibrate_every and (
                    wake_checks % CONFIG.wakeword_recalibrate_every == 0
                ):
                    recognizer.adjust_for_ambient_noise(
                        source, duration=CONFIG.ambient_adjust_duration
                    )
                if wake_failures >= CONFIG.wakeword_failure_recalibrate_after:
                    recognizer.adjust_for_ambient_noise(
                        source, duration=CONFIG.ambient_adjust_duration
                    )
                    wake_failures = 0
                audio = recognizer.listen(
                    source,
                    timeout=CONFIG.wakeword_timeout,
                    phrase_time_limit=CONFIG.wakeword_phrase_limit,
                )
                wake_checks += 1

            try:
                alternatives = _recognize_google_any(recognizer, audio)
            except sr.RequestError as e:
                logger.error("Speech recognition service error: %s", e)
                speak("Speech service error. Please check your internet connection.")
                continue
            if not alternatives:
                logger.info("Didn't catch that.")
                wake_failures += 1
                continue

            wake_failures = 0
            heard = max(alternatives, key=lambda s: len(s.split()))
            logger.info("Heard (wakecheck): %s", heard)

            # GLOBAL EXIT (works even without wake word)
            if any(is_exit_command(alt) for alt in alternatives):
                speak("Goodbye. Shutting down.")
                logger.info("Exit command received. Stopping assistant.")
                break

            if any(is_wake_word(alt) for alt in alternatives):
                logger.info("Yes Boss! How can I assist you?")
                speak("Yes boss. How can I assist you?")
                # Enter active mode; when it returns, we go back to wake-word mode
                try:
                    listen_and_respond(context=context)
                except KeyboardInterrupt:
                    logger.info("Exiting on keyboard interrupt.\n")
                    break
                # when listen_and_respond returns, we continue loop and begin wake-word detection again

        except sr.WaitTimeoutError:
            logger.info("Timeout! (no wake word detected)")
            continue
        except KeyboardInterrupt:
            print("\n")
            logger.info("Keyboard interrupt received. Stopping assistant.\n\n")
            speak("Goodbye.")
            break
        except Exception as e:
            logger.error("Error in main loop: %s", e)
            time.sleep(0.5)
            continue


if __name__ == "__main__":
    main()
