#!/bin/sh
# Build-time patch: make host-IME text arrive as ONE atomic insert.
#
# Problem
#   There is no input method inside the container, so CJK has to be composed by
#   the IME on the user's own machine, in the browser. Selkies' client then
#   flattens that composition into committed keystrokes: _compositionUpdate
#   hands every pre-edit revision to _updateCompositionText, which types the
#   revision into the remote app one keysym at a time and rewinds the previous
#   revision with real BackSpaces. The pre-edit is therefore typed into the
#   remote app live, character by character (逐字上屏) instead of the finished
#   phrase committing once.
#
#   The per-character cost is also much worse than it looks: selkies routes CJK
#   through pynput, whose from_vk() path resolves a 0x0100xxxx keysym to keycode
#   0, X returns BadValue, and the handler falls back to spawning two xdotool
#   processes per character.
#
# Fix
#   Send nothing while composing, and on compositionend send the finished string
#   once as "co,end,<text>". The server already implements that message —
#   input_handler.py turns it into a single `xdotool type <text>` — but no
#   client ever sends it (upstream only self-dispatches it for non-alphabetic
#   characters).
#
#   Linux clients keep their existing behaviour: there a separate `textInput`
#   DOM event performs the insertion, so compositionend must stay silent or the
#   text would be inserted twice.
#
# Known limit
#   input_handler.py wraps that `xdotool type` in asyncio.wait_for(..., 0.5) and
#   xdotool's default inter-key delay is 12 ms, so a single commit longer than
#   roughly 40 characters can be truncated. Normal IME phrase commits are a few
#   characters, so this is not hit in practice.
#
# Both regexes capture the minified identifiers as backreferences, because the
# copies of this code under /usr/share/selkies are mangled differently
# (e/Se in src/selkies-core.js, s/ja in assets/index-*.js).
set -e

ROOT="${1:-/usr/share/selkies}"
ID='[A-Za-z_$][A-Za-z0-9_$]*'

files=$(grep -rl '_compositionUpdate(' "$ROOT" 2>/dev/null || true)
if [ -z "$files" ]; then
    echo "atomic-ime-commit: no client bundle under $ROOT contains _compositionUpdate(" >&2
    exit 1
fi

patched=0
for f in $files; do
    sed -i \
      -e "s/_compositionUpdate(\($ID\)){this\._guac_markEvent(\1)&&this\.isComposing&&this\._updateCompositionText(\1\.data)}/_compositionUpdate(\1){this._guac_markEvent(\1)}/g" \
      -e "s/_compositionEnd(\($ID\)){if(this\._guac_markEvent(\1)&&this\.isComposing){if(\($ID\)\.isLinux()){this\._updateCompositionText(\"\"),this\.isComposing=!1,this\.compositionString=\"\";return}this\._updateCompositionText(\1\.data),this\.isComposing=!1,this\.compositionString=\"\"}}/_compositionEnd(\1){if(this._guac_markEvent(\1)\&\&this.isComposing){this.isComposing=!1,this.compositionString=\"\";var _atomicIME=\1.data||\"\";if(_atomicIME\&\&!\2.isLinux())this.send(\"co,end,\"+_atomicIME)}}/g" \
      "$f"

    # Both rewrites must land. If upstream reshapes this code the regexes stop
    # matching, and a silent no-op would ship stock 逐字上屏 behaviour while
    # looking patched — so fail the build instead.
    if grep -q '_atomicIME' "$f" &&
       grep -q "_compositionUpdate($ID){this\._guac_markEvent($ID)}" "$f"; then
        echo "atomic-ime-commit: patched $f"
        patched=$((patched + 1))
    else
        echo "atomic-ime-commit: FAILED on $f — upstream composition code changed shape" >&2
        exit 1
    fi
done

echo "atomic-ime-commit: $patched file(s) patched"
