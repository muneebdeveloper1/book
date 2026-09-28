"""Unified LLM gateway: Gemini first, Groq as fallback.

This module provides:
- Gemini-first LLM access
- retry/backoff for temporary API failures
- automatic model rotation
- Groq fallback
- strict-but-repairable JSON parsing
- protection against truncated responses
- shared budget accounting
"""

from __future__ import annotations

import ast
import json
import re
import time
from typing import Any

from src.errors import (
    AuthError,
    BudgetExceeded,
    InvalidResponseError,
    PipelineError,
    RateLimitError,
    RetryableError,
    classify_exception,
    classify_http_status,
)
from src.utils import http
from src.utils.log import get_logger

log = get_logger("llm")


def _classify_gemini(exc: Exception) -> PipelineError | None:
    """Convert Gemini/http exceptions into the pipeline's error classes."""

    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)

    if isinstance(code, int):
        return classify_http_status(code, str(exc))

    return classify_exception(exc)


def _is_json_compatible(value: Any) -> bool:
    """Return True when the value can be serialized as JSON."""

    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False


class Gemini:
    def __init__(
        self,
        settings,
        budget,
        gemini_key: str = "",
        groq_key: str = "",
        client=None,
        http_session=None,
        sleep=time.sleep,
    ):
        self.cfg = settings.llm
        self.budget = budget

        self.gemini_key = (gemini_key or "").strip()
        self.groq_key = (groq_key or "").strip()

        if not self.gemini_key and not self.groq_key and client is None:
            raise AuthError("Set GEMINI_API_KEY or GROQ_API_KEY")

        self.client = client

        if self.client is None and self.gemini_key:
            from google import genai

            self.client = genai.Client(api_key=self.gemini_key)

        self.http = http_session or http.session()
        self._sleep = sleep

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def text(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.5,
    ) -> str:
        return self._complete(
            prompt,
            system,
            max_tokens,
            temperature,
            json_mode=False,
        )

    def json(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 8192,
        temperature: float = 0.2,
        attempts: int | None = None,
    ) -> Any:
        """Request JSON and never accept malformed output.

        Important behavior:

        Gemini model A
            -> temporary error -> retry
            -> malformed JSON -> next model

        Gemini model B
            -> temporary error -> retry
            -> malformed JSON -> next model

        All Gemini models exhausted
            -> Groq fallback

        If a provider returns a common bullet-list representation of a
        string array, _parse_json() repairs it.
        """

        if attempts is None:
            # Use the configured retry count, but always allow at least
            # three parsing/provider attempts.
            attempts = max(3, int(getattr(self.cfg, "max_retries", 2)) + 1)

        system_text = (
            (system or "")
            + "\nReturn ONLY valid JSON."
            + "\nDo not use markdown fences."
            + "\nDo not add commentary."
            + "\nIf the requested result is an array, return a JSON array."
        )

        last: Exception | None = None

        for attempt in range(attempts):
            if attempt == 0:
                hint = ""
            else:
                hint = (
                    "\n\nIMPORTANT: Your previous response could not be "
                    "parsed as JSON. Return strict JSON only."
                    "\nDo not use YAML."
                    "\nDo not use bullet points."
                    "\nDo not use explanations."
                )

            try:
                raw = self._complete(
                    prompt + hint,
                    system_text,
                    max_tokens,
                    temperature,
                    json_mode=True,
                )

                parsed = self._parse_json(raw)

                if not _is_json_compatible(parsed):
                    raise InvalidResponseError(
                        "LLM returned a value that cannot be represented as JSON"
                    )

                return parsed

            except BudgetExceeded:
                raise

            except InvalidResponseError as exc:
                last = exc

                log.warning(
                    "invalid JSON from LLM; retrying",
                    attempt=attempt + 1,
                    error=str(exc)[:220],
                )

                if attempt < attempts - 1:
                    self._sleep(
                        min(
                            self.cfg.retry_base_seconds
                            * (2 ** min(attempt, 5)),
                            30,
                        )
                    )

            except PipelineError as exc:
                last = exc

                if not exc.retryable:
                    raise

                log.warning(
                    "JSON LLM request failed; retrying",
                    attempt=attempt + 1,
                    error=str(exc)[:220],
                )

                if attempt < attempts - 1:
                    delay = (
                        exc.retry_after
                        if isinstance(exc, RateLimitError)
                        and exc.retry_after
                        else self.cfg.retry_base_seconds
                        * (2 ** min(attempt, 5))
                    )

                    self._sleep(min(delay, 60))

        if last is not None:
            raise last

        raise RetryableError("JSON LLM request failed without a usable response")

    def vision_json(
        self,
        prompt: str,
        images: list[bytes],
        mime: str = "image/jpeg",
        max_tokens: int = 4000,
    ) -> Any:
        """Ask Gemini about images and parse a JSON answer."""

        if not self.client:
            raise AuthError("Vision requires GEMINI_API_KEY")

        from google.genai import types

        parts: list[Any] = [
            prompt
            + "\nReturn ONLY valid JSON."
            + "\nNo markdown fences."
            + "\nNo commentary."
        ]

        parts += [
            types.Part.from_bytes(data=img, mime_type=mime)
            for img in images
        ]

        last: Exception | None = None

        models = list(self.cfg.gemini_models or [])

        if not models:
            raise RetryableError("No Gemini models configured for vision")

        for model in models:
            for attempt in range(self.cfg.max_retries + 1):
                try:
                    self.budget.spend("gemini_calls")

                    response = self.client.models.generate_content(
                        model=model,
                        contents=parts,
                        config={
                            "temperature": 0.1,
                            "max_output_tokens": max_tokens,
                            "response_mime_type": "application/json",
                        },
                    )

                    text = getattr(response, "text", None)

                    if not text or not text.strip():
                        raise InvalidResponseError(
                            f"{model} returned empty vision output"
                        )

                    self._check_finish_reason(
                        response,
                        model,
                        max_tokens,
                    )

                    parsed = self._parse_json(text)

                    log.info(
                        "vision llm ok",
                        provider="gemini",
                        model=model,
                    )

                    return parsed

                except BudgetExceeded:
                    raise

                except Exception as exc:
                    classified = (
                        exc
                        if isinstance(exc, PipelineError)
                        else _classify_gemini(exc)
                    )

                    if classified is None:
                        raise

                    last = classified

                    if isinstance(classified, AuthError):
                        log.warning(
                            "gemini vision auth error; next model",
                            model=model,
                        )
                        break

                    if isinstance(classified, InvalidResponseError):
                        log.warning(
                            "gemini vision returned unusable output; "
                            "next model",
                            model=model,
                            error=str(classified)[:180],
                        )
                        break

                    if not classified.retryable:
                        raise classified

                    if attempt < self.cfg.max_retries:
                        delay = (
                            classified.retry_after
                            if isinstance(classified, RateLimitError)
                            and classified.retry_after
                            else self.cfg.retry_base_seconds
                            * (2 ** min(attempt, 5))
                        )

                        self._sleep(min(delay, 60))

                    else:
                        log.warning(
                            "gemini vision model failed; next model",
                            model=model,
                            error=str(classified)[:180],
                        )

        raise RetryableError(
            f"Vision request failed on all Gemini models: {last}"
        )

    # ------------------------------------------------------------------
    # Main provider routing
    # ------------------------------------------------------------------

    def _complete(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        temperature: float,
        json_mode: bool,
    ) -> str:
        """Execute Gemini first, then Groq.

        A malformed JSON response is treated as a provider failure when
        json_mode=True, allowing the next model/provider to be attempted.
        """

        last: Exception | None = None

        # --------------------------------------------------------------
        # GEMINI
        # --------------------------------------------------------------

        if self.client:
            for model in self.cfg.gemini_models:

                for attempt in range(self.cfg.max_retries + 1):
                    try:
                        return self._gemini_once(
                            model,
                            prompt,
                            system,
                            max_tokens,
                            temperature,
                            json_mode,
                        )

                    except BudgetExceeded:
                        raise

                    except Exception as exc:
                        classified = (
                            exc
                            if isinstance(exc, PipelineError)
                            else _classify_gemini(exc)
                        )

                        if classified is None:
                            raise

                        last = classified

                        # --------------------------------------------------
                        # Invalid JSON
                        #
                        # Do NOT keep retrying the same model forever.
                        # Move to the next configured model.
                        # --------------------------------------------------
                        if isinstance(
                            classified,
                            InvalidResponseError,
                        ):
                            log.warning(
                                "gemini model returned unusable response; "
                                "next model",
                                model=model,
                                error=str(classified)[:220],
                            )
                            break

                        # --------------------------------------------------
                        # Authentication
                        # --------------------------------------------------
                        if isinstance(classified, AuthError):
                            log.warning(
                                "gemini auth/permission error; next model",
                                model=model,
                            )
                            break

                        # --------------------------------------------------
                        # Permanent failure
                        # --------------------------------------------------
                        if not classified.retryable:
                            log.warning(
                                "gemini permanent failure; next model",
                                model=model,
                                error=str(classified)[:220],
                            )
                            break

                        # --------------------------------------------------
                        # Retry temporary failure
                        #
                        # Includes:
                        # - HTTP 429
                        # - HTTP 500
                        # - HTTP 502
                        # - HTTP 503
                        # - HTTP 504
                        # - connection failures
                        # - RemoteProtocolError when classified retryable
                        # --------------------------------------------------
                        if attempt < self.cfg.max_retries:
                            delay = (
                                classified.retry_after
                                if isinstance(classified, RateLimitError)
                                and classified.retry_after
                                else self.cfg.retry_base_seconds
                                * (2 ** min(attempt, 5))
                            )

                            log.warning(
                                "gemini temporary failure; retrying",
                                model=model,
                                attempt=attempt + 1,
                                delay=round(delay, 2),
                                error=str(classified)[:180],
                            )

                            self._sleep(min(delay, 60))

                        else:
                            log.warning(
                                "gemini model failed; next model",
                                model=model,
                                error=str(classified)[:180],
                            )

        # --------------------------------------------------------------
        # GROQ FALLBACK
        # --------------------------------------------------------------

        if self.groq_key:
            for model in self.cfg.groq_models:
                for attempt in range(self.cfg.max_retries + 1):
                    try:
                        raw = self._groq(
                            model,
                            prompt,
                            system,
                            max_tokens,
                            temperature,
                            json_mode,
                        )

                        # Validate JSON here so malformed Groq output does
                        # not get returned to the caller.
                        if json_mode:
                            self._parse_json(raw)

                        return raw

                    except BudgetExceeded:
                        raise

                    except Exception as exc:
                        classified = (
                            exc
                            if isinstance(exc, PipelineError)
                            else classify_exception(exc)
                        )

                        if classified is None:
                            raise

                        last = classified

                        if isinstance(
                            classified,
                            InvalidResponseError,
                        ):
                            log.warning(
                                "groq returned invalid JSON; next model",
                                model=model,
                                error=str(classified)[:180],
                            )
                            break

                        if not classified.retryable:
                            log.warning(
                                "groq permanent failure; next model",
                                model=model,
                                error=str(classified)[:180],
                            )
                            break

                        if attempt < self.cfg.max_retries:
                            delay = (
                                classified.retry_after
                                if isinstance(classified, RateLimitError)
                                and classified.retry_after
                                else self.cfg.retry_base_seconds
                                * (2 ** min(attempt, 5))
                            )

                            self._sleep(min(delay, 60))

                        else:
                            log.warning(
                                "groq model failed; next fallback",
                                model=model,
                                error=str(classified)[:180],
                            )

        if isinstance(last, PipelineError) and not last.retryable:
            raise last

        raise RetryableError(
            f"All writing LLMs failed. Last error: {last}"
        )

    # ------------------------------------------------------------------
    # Gemini request
    # ------------------------------------------------------------------

    def _gemini_once(
        self,
        model,
        prompt,
        system,
        max_tokens,
        temperature,
        json_mode,
    ) -> str:
        self.budget.spend("gemini_calls")

        contents = (
            f"{system}\n\n{prompt}"
            if system
            else prompt
        )

        config: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }

        if json_mode:
            config["response_mime_type"] = "application/json"

        response = self.client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

        text = getattr(response, "text", None)

        if not text or not text.strip():
            raise InvalidResponseError(
                f"Gemini {model} returned empty output"
            )

        self._check_finish_reason(
            response,
            model,
            max_tokens,
        )

        text = text.strip()

        # --------------------------------------------------------------
        # IMPORTANT:
        #
        # Validate JSON before returning it.
        #
        # This is what allows malformed Gemini output to trigger model
        # rotation instead of killing the entire pipeline.
        # --------------------------------------------------------------

        if json_mode:
            self._parse_json(text)

        log.info(
            "llm ok",
            provider="gemini",
            model=model,
        )

        return text

    # ------------------------------------------------------------------
    # Groq request
    # ------------------------------------------------------------------

    def _groq(
        self,
        model,
        prompt,
        system,
        max_tokens,
        temperature,
        json_mode,
    ) -> str:
        self.budget.spend("gemini_calls")

        messages = []

        if system:
            messages.append(
                {
                    "role": "system",
                    "content": system,
                }
            )

        messages.append(
            {
                "role": "user",
                "content": prompt,
            }
        )

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Do NOT force Groq's json_object response format here.
        #
        # A visual-query request asks for a JSON ARRAY.
        # Some OpenAI-compatible JSON modes are object-oriented and can
        # reject array responses.
        #
        # The prompt + _parse_json() validation is safer for this project.

        resp = self.http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            timeout=self.cfg.request_timeout,
            headers={
                "Authorization": f"Bearer {self.groq_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )

        http.check_status(
            resp,
            f"groq {model}",
        )

        choice = (
            (resp.json().get("choices") or [{}])[0]
        )

        content = (
            (choice.get("message") or {}).get(
                "content",
                "",
            )
        )

        if not content:
            raise InvalidResponseError(
                "Groq returned empty content"
            )

        if choice.get("finish_reason") == "length":
            raise InvalidResponseError(
                f"Groq {model} output was truncated"
            )

        if json_mode:
            self._parse_json(content)

        log.info(
            "llm ok",
            provider="groq",
            model=model,
        )

        return content.strip()

    # ------------------------------------------------------------------
    # Response validation
    # ------------------------------------------------------------------

    @staticmethod
    def _check_finish_reason(
        response,
        model: str,
        max_tokens: int,
    ) -> None:
        reason = ""

        try:
            reason = str(
                response.candidates[0].finish_reason
            )
        except (
            AttributeError,
            IndexError,
            TypeError,
        ):
            pass

        if "MAX_TOKENS" in reason.upper():
            raise InvalidResponseError(
                f"Gemini {model} output was truncated "
                f"(max_tokens={max_tokens})"
            )

    # ------------------------------------------------------------------
    # JSON parser / repair
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_json(raw: str) -> Any:
        """Parse strict JSON and repair common LLM formatting mistakes.

        Supported:
        - normal JSON
        - markdown JSON fences
        - JSON embedded in surrounding text
        - Python-style dict/list literals
        - bullet-list output such as:

              - first query
              - second query
              - third query

        The last case is important because that is exactly what your
        failed run returned.
        """

        text = (raw or "").strip()

        if not text:
            raise InvalidResponseError(
                "Invalid JSON from LLM: empty response"
            )

        # --------------------------------------------------------------
        # Remove markdown fences
        # --------------------------------------------------------------

        if text.startswith("```"):
            lines = text.splitlines()

            if lines:
                lines = lines[1:]

            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]

            text = "\n".join(lines).strip()

        # --------------------------------------------------------------
        # 1. Direct JSON
        # --------------------------------------------------------------

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # --------------------------------------------------------------
        # 2. Extract JSON object/array embedded in text
        # --------------------------------------------------------------

        candidates: list[str] = []

        for open_ch, close_ch in (
            ("{", "}"),
            ("[", "]"),
        ):
            start = text.find(open_ch)
            end = text.rfind(close_ch)

            if 0 <= start < end:
                candidates.append(
                    text[start : end + 1]
                )

        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

        # --------------------------------------------------------------
        # 3. Python literal fallback
        #
        # Useful when an LLM returns:
        #
        # ['query one', 'query two']
        #
        # instead of strict JSON.
        # --------------------------------------------------------------

        try:
            parsed = ast.literal_eval(text)

            if isinstance(parsed, (dict, list)):
                return parsed

        except (SyntaxError, ValueError):
            pass

        # --------------------------------------------------------------
        # 4. Repair common bullet-list output
        #
        # Example from your failed run:
        #
        # [
        #   - person looking...
        #   - car dealership...
        #   - individual reviewing...
        # ]
        #
        # This is NOT valid JSON, but it is obviously an intended list
        # of strings. Convert it into a real Python list.
        # --------------------------------------------------------------

        bullet_items: list[str] = []

        for line in text.splitlines():
            cleaned = line.strip()

            # Ignore wrapper brackets
            if cleaned in ("[", "]"):
                continue

            # Match:
            # -
            # *
            # •
            # numbered lists: 1. / 2) etc.
            match = re.match(
                r"^(?:[-*•]\s+|\d+[.)]\s+)(.+?)\s*$",
                cleaned,
            )

            if not match:
                continue

            item = match.group(1).strip()

            # Remove accidental surrounding quotes.
            if (
                len(item) >= 2
                and item[0] in "\"'"
                and item[-1] == item[0]
            ):
                item = item[1:-1].strip()

            if item:
                bullet_items.append(item)

        if bullet_items:
            return bullet_items

        # --------------------------------------------------------------
        # 5. Last-resort single-line list repair.
        #
        # Only use when the text clearly contains comma-separated
        # quoted strings.
        # --------------------------------------------------------------

        if (
            text.startswith("[")
            and text.endswith("]")
            and "," in text
        ):
            inner = text[1:-1].strip()

            parts = [
                p.strip().strip("\"'")
                for p in inner.split(",")
                if p.strip()
            ]

            if parts and all(parts):
                return parts

        raise InvalidResponseError(
            f"Invalid JSON from LLM: {text[:500]}"
        )
