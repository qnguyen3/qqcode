#!/usr/bin/env bash

# QQcode Installation Script
# This script installs uv if not present and then installs qqcode using uv

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

function error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

function info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

function success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

function warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

function check_platform() {
    local platform=$(uname -s)

    if [[ "$platform" == "Linux" ]]; then
        info "Detected Linux platform"
        PLATFORM="linux"
    elif [[ "$platform" == "Darwin" ]]; then
        info "Detected macOS platform"
        PLATFORM="macos"
    else
        error "Unsupported platform: $platform"
        error "This installation script currently only supports Linux and macOS"
        exit 1
    fi
}

function check_uv_installed() {
    if command -v uv &> /dev/null; then
        info "uv is already installed: $(uv --version)"
        UV_INSTALLED=true
    else
        info "uv is not installed"
        UV_INSTALLED=false
    fi
}

function install_uv() {
    info "Installing uv using the official Astral installer..."

    if ! command -v curl &> /dev/null; then
        error "curl is required to install uv. Please install curl first."
        exit 1
    fi

    if curl -LsSf https://astral.sh/uv/install.sh | sh; then
        success "uv installed successfully"

        export PATH="$HOME/.local/bin:$PATH"

        if ! command -v uv &> /dev/null; then
            warning "uv was installed but not found in PATH for this session"
            warning "You may need to restart your terminal or run:"
            warning "  export PATH=\"\$HOME/.cargo/bin:\$HOME/.local/bin:\$PATH\""
        fi
    else
        error "Failed to install uv"
        exit 1
    fi
}

function install_qqcode() {
    info "Installing qqcode from PyPI using uv..."
    uv tool install qqcode

    success "QQcode installed successfully! (commands: qqcode, qqcode-acp)"
}

function main() {
    echo
    echo "  ▇▇▇▇      ▇▇▇▇  "
    echo "▇▇    ▇▇  ▇▇    ▇▇"
    echo "▇▇    ▇▇  ▇▇    ▇▇"
    echo "▇▇  ▇▇▇▇  ▇▇  ▇▇▇▇"
    echo "  ▇▇▇▇▇▇    ▇▇▇▇▇▇"
    echo
    echo "Starting QQcode installation..."
    echo

    check_platform

    check_uv_installed

    if [[ "$UV_INSTALLED" == "false" ]]; then
        install_uv
    fi

    install_qqcode

    if command -v qqcode &> /dev/null; then
        success "Installation completed successfully!"
        echo
        echo "You can now run qqcode with:"
        echo "  qqcode"
        echo
        echo "Or for ACP mode:"
        echo "  qqcode-acp"
    else
        error "Installation completed but 'qqcode' command not found"
        error "Please check your installation and PATH settings"
        exit 1
    fi
}

main
