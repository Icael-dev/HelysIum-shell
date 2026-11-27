#!/usr/bin/env bash
set -euo pipefail

# HelysIum-shell automated Arch Linux installer from a GitHub clone.
# Uses packaging/arch/PKGBUILD as the single source of truth:
# extracts dependencies via `makepkg --printsrcinfo` into a temporary file,
# installs missing official/AUR dependencies (including libcava,
# qt6-m3shapes-git, quickshell-git) via yay or paru, and builds/installs
# the local package with `makepkg -Cfsi`.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PKGBUILD_DIR="${SCRIPT_DIR}/packaging/arch"
OS_RELEASE_FILE="${HELYSIUM_OS_RELEASE:-/etc/os-release}"
AUR_BASE_URL="${HELYSIUM_AUR_BASE_URL:-https://aur.archlinux.org}"

TMP_DIR=""
TMP_SRCINFO=""

cleanup() {
    if [[ -n "$TMP_SRCINFO" && -f "$TMP_SRCINFO" ]]; then
        rm -f -- "$TMP_SRCINFO"
    fi
    if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
        rm -rf -- "$TMP_DIR"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

info() {
    printf ':: %s\n' "$*"
}

warn() {
    printf ':: Warning: %s\n' "$*" >&2
}

err() {
    printf ':: Error: %s\n' "$*" >&2
}

confirm_prompt() {
    local prompt="$1"
    local reply=""
    printf ':: %s [y/N] ' "$prompt"
    if ! IFS= read -r reply; then
        printf '\n'
        return 1
    fi
    case "${reply,,}" in
        y|yes) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Step A: Initial checks ---

if [[ "${EUID:-$(id -u)}" -eq 0 || "${HELYSIUM_TEST_EUID:-}" == "0" ]]; then
    err "Do not run install.sh as root (makepkg refuses root execution). Run as a regular user."
    exit 1
fi

if [[ ! -f "${PKGBUILD_DIR}/PKGBUILD" ]]; then
    err "PKGBUILD not found at ${PKGBUILD_DIR}/PKGBUILD. Run install.sh from the HelysIum-shell repository."
    exit 1
fi

is_arch_compatible() {
    if [[ -r "$OS_RELEASE_FILE" ]]; then
        local id_val="" id_like_val=""
        id_val="$(grep -E '^ID=' "$OS_RELEASE_FILE" | head -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
        id_like_val="$(grep -E '^ID_LIKE=' "$OS_RELEASE_FILE" | head -n 1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
        if [[ "$id_val" == "arch" || "$id_like_val" == *arch* ]]; then
            return 0
        fi
    fi
    command -v pacman >/dev/null 2>&1
}

if ! is_arch_compatible; then
    err "Unsupported operating system: Arch Linux (or an Arch-based distribution with pacman) is required."
    exit 1
fi

if ! command -v pacman >/dev/null 2>&1; then
    err "Required tool 'pacman' not found in PATH."
    exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
    err "Required tool 'sudo' not found in PATH. Install and configure sudo first."
    exit 1
fi

missing_prereqs=()
if ! command -v git >/dev/null 2>&1; then
    missing_prereqs+=("git")
fi
if ! command -v makepkg >/dev/null 2>&1 || ! pacman -Qq base-devel >/dev/null 2>&1; then
    missing_prereqs+=("base-devel")
fi

if (( ${#missing_prereqs[@]} > 0 )); then
    warn "Missing official prerequisites: ${missing_prereqs[*]}"
    if ! confirm_prompt "Install missing prerequisites (${missing_prereqs[*]}) via 'sudo pacman -S --needed'?"; then
        info "Installation cancelled."
        exit 1
    fi
    info "Installing prerequisites: ${missing_prereqs[*]}"
    if ! sudo pacman -S --needed "${missing_prereqs[@]}"; then
        err "Failed to install prerequisites (${missing_prereqs[*]}) via pacman."
        exit 1
    fi
fi

# --- Step B: AUR helper detection & selection ---

HAS_YAY=0
HAS_PARU=0
command -v yay >/dev/null 2>&1 && HAS_YAY=1
command -v paru >/dev/null 2>&1 && HAS_PARU=1

AUR_HELPER=""
NEED_BOOTSTRAP=0

if (( HAS_YAY == 1 && HAS_PARU == 0 )); then
    AUR_HELPER="yay"
    info "Detected AUR helper: yay"
elif (( HAS_PARU == 1 && HAS_YAY == 0 )); then
    AUR_HELPER="paru"
    info "Detected AUR helper: paru"
elif (( HAS_YAY == 1 && HAS_PARU == 1 )); then
    info "Both 'yay' and 'paru' are installed."
    printf ':: Select AUR helper to use [1=yay (default), 2=paru, q=cancel]: '
    choice=""
    if ! IFS= read -r choice; then
        printf '\n'
        info "Installation cancelled."
        exit 1
    fi
    case "${choice,,}" in
        ""|1|yay) AUR_HELPER="yay" ;;
        2|paru) AUR_HELPER="paru" ;;
        q|quit|cancel|n|no)
            info "Installation cancelled."
            exit 0
            ;;
        *)
            err "Invalid selection '${choice}'. Expected 1 (yay), 2 (paru), or q (cancel)."
            exit 1
            ;;
    esac
    info "Selected AUR helper: ${AUR_HELPER}"
else
    warn "No AUR helper (yay or paru) is currently installed."
    printf ':: Choose an AUR helper to install from the official AUR [1=yay, 2=paru, q=cancel]: '
    choice=""
    if ! IFS= read -r choice; then
        printf '\n'
        info "Installation cancelled."
        exit 1
    fi
    case "${choice,,}" in
        1|yay)
            AUR_HELPER="yay"
            NEED_BOOTSTRAP=1
            ;;
        2|paru)
            AUR_HELPER="paru"
            NEED_BOOTSTRAP=1
            ;;
        ""|q|quit|cancel|n|no)
            info "Installation cancelled."
            exit 0
            ;;
        *)
            err "Invalid selection '${choice}'. Expected 1 (yay), 2 (paru), or q (cancel)."
            exit 1
            ;;
    esac
fi

# --- Step C: Bootstrap chosen AUR helper ---

if (( NEED_BOOTSTRAP == 1 )); then
    helper_url="${AUR_BASE_URL}/${AUR_HELPER}.git"
    if ! confirm_prompt "Clone and install '${AUR_HELPER}' from ${helper_url} using 'makepkg -si' (requires sudo)?"; then
        info "Installation cancelled."
        exit 0
    fi

    TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/helysium-aur-helper.XXXXXX")"
    info "Cloning ${AUR_HELPER} from ${helper_url}..."
    if ! git clone --depth=1 "$helper_url" "${TMP_DIR}/${AUR_HELPER}"; then
        err "Failed to clone AUR helper '${AUR_HELPER}' from ${helper_url}."
        exit 1
    fi

    info "Building and installing ${AUR_HELPER} with makepkg -si..."
    if ! (cd "${TMP_DIR}/${AUR_HELPER}" && makepkg -si); then
        err "Failed to build or install AUR helper '${AUR_HELPER}'."
        exit 1
    fi

    if ! command -v "$AUR_HELPER" >/dev/null 2>&1; then
        err "AUR helper '${AUR_HELPER}' was built, but is still not found in PATH."
        exit 1
    fi
    info "AUR helper '${AUR_HELPER}' is ready."
fi

# --- Step D: Extract PKGBUILD dependencies via temporary .SRCINFO, install them via helper, then makepkg -Cfsi ---

HOST_ARCH="$(uname -m)"
extract_srcinfo_deps() {
    local srcinfo_file="$1"
    local line key val
    local -A seen=()
    while IFS= read -r line; do
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        [[ "$line" == *=* ]] || continue
        key="${line%%=*}"
        val="${line#*=}"
        key="${key%"${key##*[![:space:]]}"}"
        val="${val#"${val%%[![:space:]]*}"}"
        val="${val%"${val##*[![:space:]]}"}"
        [[ -z "$val" ]] && continue
        case "$key" in
            depends|makedepends|checkdepends|"depends_${HOST_ARCH}"|"makedepends_${HOST_ARCH}"|"checkdepends_${HOST_ARCH}")
                if [[ -z "${seen[$val]+x}" ]]; then
                    seen["$val"]=1
                    printf '%s\n' "$val"
                fi
                ;;
        esac
    done < "$srcinfo_file"
}

TMP_SRCINFO="$(mktemp "${TMPDIR:-/tmp}/helysium-shell-srcinfo.XXXXXX")"

info "Generating temporary package metadata (.SRCINFO) from PKGBUILD..."
if ! (cd "$PKGBUILD_DIR" && makepkg --printsrcinfo) > "$TMP_SRCINFO"; then
    err "Failed to generate .SRCINFO via 'makepkg --printsrcinfo' in ${PKGBUILD_DIR}."
    exit 1
fi

if [[ ! -s "$TMP_SRCINFO" ]] || ! grep -Eq '^[[:space:]]*pkgbase[[:space:]]*=' "$TMP_SRCINFO" || ! grep -Eq '^[[:space:]]*pkgname[[:space:]]*=' "$TMP_SRCINFO"; then
    err "Generated .SRCINFO is empty or invalid (missing pkgbase/pkgname metadata)."
    exit 1
fi

all_pkg_deps=()
while IFS= read -r dep; do
    [[ -n "$dep" ]] && all_pkg_deps+=("$dep")
done < <(extract_srcinfo_deps "$TMP_SRCINFO")

missing_pkg_deps=()
if (( ${#all_pkg_deps[@]} > 0 )); then
    t_rc=0
    t_out="$(pacman -T "${all_pkg_deps[@]}" 2>/dev/null)" || t_rc=$?
    if (( t_rc == 0 )); then
        info "All PKGBUILD dependencies are already satisfied."
    elif (( t_rc == 127 )); then
        while IFS= read -r dep; do
            [[ -n "$dep" ]] && missing_pkg_deps+=("$dep")
        done <<< "$t_out"
    else
        err "Failed to check dependencies with 'pacman -T' (exit code ${t_rc})."
        exit 1
    fi
fi

# Run helper and makepkg in the foreground so stdin stays attached to the user's terminal
# for interactive prompts (and yay cleanBuild never touches packaging/arch/).
if (( ${#missing_pkg_deps[@]} > 0 )); then
    info "Installing missing PKGBUILD dependencies with '${AUR_HELPER}': ${missing_pkg_deps[*]}"
    dep_rc=0
    "$AUR_HELPER" -S --needed --asdeps "${missing_pkg_deps[@]}" || dep_rc=$?
    if (( dep_rc != 0 )); then
        err "Failed to resolve or install dependencies (${missing_pkg_deps[*]}) with '${AUR_HELPER}' (exit code ${dep_rc})."
        exit "$dep_rc"
    fi
fi

info "Building and installing HelysIum-shell from ${PKGBUILD_DIR} using 'makepkg -Cfsi'..."
rc=0
(cd "$PKGBUILD_DIR" && makepkg -Cfsi) || rc=$?

# --- Step E: Result ---

if (( rc != 0 )); then
    err "Failed to build or install HelysIum-shell with 'makepkg -Cfsi' (exit code ${rc})."
    err "Inspect the output above and retry once any build or dependency issue is resolved."
    exit "$rc"
fi

info "HelysIum-shell has been built and installed successfully."
info "Run 'helysium --version' or 'helysium shell -d' to verify your installation."
