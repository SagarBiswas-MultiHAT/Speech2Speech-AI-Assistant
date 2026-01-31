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
USE_POWERSHELL_TTS = os.name == "nt"
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
LISTEN_TIMEOUT = 25          # how long listen() waits for phrase to start
PHRASE_TIME_LIMIT = 8        # maximum seconds per phrase
WAKEWORD_TIMEOUT = 4         # timeout while waiting for wake word
WAKEWORD_PHRASE_LIMIT = 1.8  # max seconds to capture wake-word phrase
AMBIENT_ADJUST_DURATION = 0.4
WAKEWORD_RECALIBRATE_EVERY = 0  # 0 disables periodic recalibration
FAILURE_RECALIBRATE_AFTER = 2   # recalibrate after consecutive failures

# ---------- TTS Setup ----------
engine = None
if not USE_POWERSHELL_TTS:
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
    if USE_POWERSHELL_TTS:
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
        recognizer.pause_threshold = 0.5
        recognizer.non_speaking_duration = 0.2
        recognizer.phrase_threshold = 0.2
        recognizer.dynamic_energy_adjustment_damping = 0.12
        recognizer.dynamic_energy_ratio = 1.4
    else:
        # Be more patient in active mode to avoid cutting users off mid-sentence
        recognizer.pause_threshold = 1.0
        recognizer.non_speaking_duration = 0.4
        recognizer.phrase_threshold = 0.4
        recognizer.dynamic_energy_adjustment_damping = 0.15
        recognizer.dynamic_energy_ratio = 1.6


# ---------- Utilities ----------
GOODBYE_TERMS = {"good bye", "goodbye", "bye", "exit", "quit", "stop"}


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


# ---------- AI integration (optional) ----------
_groq_client = None
if Groq and GROQ_API_KEY:
    try:
        _groq_client = Groq(api_key=GROQ_API_KEY)
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
            model=GROQ_MODEL,
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
    try:
        webbrowser.open_new_tab(url)
    except Exception as e:
        print("Could not open browser:", e)


def _recognize_google_any(recognizer: sr.Recognizer, audio) -> list:
    """
    Return a list of possible transcripts from Google Speech.
    """
    try:
        result = recognizer.recognize_google(audio, show_all=True)
    except Exception:
        return []

    if isinstance(result, str):
        return [result]
    if isinstance(result, dict):
        alts = result.get("alternative") or []
        return [a.get("transcript", "") for a in alts if a.get("transcript")]
    return []

def open_vscode():
    """
    Open Visual Studio Code using an absolute path (Windows).
    """
    vscode_path = r"C:\Users\sagar\AppData\Local\Programs\Microsoft VS Code\Code.exe"

    if not os.path.exists(vscode_path):
        speak("Visual Studio Code is not installed in the expected location.")
        return

    try:
        subprocess.Popen([vscode_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print("Error opening VS Code:", e)
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
        print("Error closing VS Code:", e)
        speak("Sorry, I couldn't close Visual Studio Code.")



def prossesCommand(c: str, context=None):
    """
    Process a single spoken command.
    Returns (context, exit_to_wake) where exit_to_wake=True means user said goodbye.
    """
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
                speak(f"I don't have {song} in the library. Opening a web search.")
                _open_url("https://www.youtube.com/results?search_query=" + quote(song))
            return context, False
        except Exception as e:
            print("Play command error:", e)
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
            print("Search command error:", e)
            return context, False

    # Goodbye handling
    if _is_goodbye(command):
        output = "Goodbye! It was nice assisting you. Say 'hey sagar' when you need me."
        print("\nAI Response:", output)
        speak(output)
        if context is None:
            context = []
        context.append({"role": "user", "content": command})
        context.append({"role": "assistant", "content": output})
        return context, True

    # Otherwise, fallback to AI processing (or fallback string)
    output = aiProcess(command, context)
    print("\nAI Response:", output)
    speak(output)

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
                if failures >= FAILURE_RECALIBRATE_AFTER:
                    recognizer.adjust_for_ambient_noise(source, duration=AMBIENT_ADJUST_DURATION)
                    failures = 0
                print("\n--> Sagar listening...")
                audio = recognizer.listen(source, timeout=LISTEN_TIMEOUT, phrase_time_limit=PHRASE_TIME_LIMIT)
                try:
                    command = recognizer.recognize_google(audio)
                except sr.UnknownValueError:
                    # Could not parse audio
                    print("\nSorry, I didn't catch that.")
                    failures += 1
                    continue
                except sr.RequestError as e:
                    # API/service error (network or service)
                    print("\nSpeech recognition service error:", e)
                    speak("Speech service error. Please check your internet connection.")
                    continue

                failures = 0
                print("\nCommand:", command)
                context, exit_to_wake = prossesCommand(command, context)
                if exit_to_wake:
                    # user said goodbye; return to wake-word mode
                    return True

        except sr.WaitTimeoutError:
            # Nothing said; go back to listening in active mode
            print("\nListening timed out (no speech detected).")
            continue
        except KeyboardInterrupt:
            print("\nKeyboard interrupt received. Exiting.\n")
            speak("Goodbye.\n")
            raise
        except Exception as e:
            print("Unexpected error in active listening:", e)
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


# ---------- Main (wake-word) loop ----------
def main():
    context = None
    recognizer = sr.Recognizer()
    _configure_recognizer(recognizer, mode="wake")
    wake_checks = 0
    wake_failures = 0
    mic = sr.Microphone()

    print("\nSagar voice assistant starting. Say 'hey sagar' to activate.")
    speak("Sagar voice assistant starting. Say 'hey sagar' to activate.")

    # Initial ambient calibration to reduce false negatives
    try:
        with sr.Microphone() as source:
            print("Calibrating microphone...")
            recognizer.adjust_for_ambient_noise(source, duration=AMBIENT_ADJUST_DURATION)
    except Exception as e:
        print("Microphone calibration error:", e)

    while True:
        print("\nRecognizing...")
        try:
            with mic as source:
                print("Listening...")
                # Recalibrate occasionally or after consecutive failures
                if WAKEWORD_RECALIBRATE_EVERY and (wake_checks % WAKEWORD_RECALIBRATE_EVERY == 0):
                    recognizer.adjust_for_ambient_noise(source, duration=AMBIENT_ADJUST_DURATION)
                if wake_failures >= FAILURE_RECALIBRATE_AFTER:
                    recognizer.adjust_for_ambient_noise(source, duration=AMBIENT_ADJUST_DURATION)
                    wake_failures = 0
                audio = recognizer.listen(
                    source,
                    timeout=WAKEWORD_TIMEOUT,
                    phrase_time_limit=WAKEWORD_PHRASE_LIMIT
                )
                wake_checks += 1

            alternatives = _recognize_google_any(recognizer, audio)
            if not alternatives:
                print("Didn't catch that.")
                wake_failures += 1
                continue

            wake_failures = 0
            heard = alternatives[0]
            print("\nHeard (wakecheck):", heard)

            # GLOBAL EXIT (works even without wake word)
            if any(is_exit_command(alt) for alt in alternatives):
                speak("Goodbye. Shutting down.")
                print("Exit command received. Stopping assistant.\n")
                break

            if any(is_wake_word(alt) for alt in alternatives):
                print("\nYes Boss! How can I assist you?")
                speak("Yes boss. How can I assist you?")
                # Enter active mode; when it returns, we go back to wake-word mode
                try:
                    listen_and_respond(context=context)
                except KeyboardInterrupt:
                    print("Exiting on keyboard interrupt.\n\n")
                    break
                # when listen_and_respond returns, we continue loop and begin wake-word detection again

        except sr.WaitTimeoutError:
            print("\nTimeout! (no wake word detected)\n")
            continue
        except KeyboardInterrupt:
            print("\nKeyboard interrupt received. Stopping assistant.\n")
            speak("Goodbye.\n")
            break
        except Exception as e:
            print("\nError in main loop:\n", e)
            time.sleep(0.5)
            continue


if __name__ == "__main__":
    main()
