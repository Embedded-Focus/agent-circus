/* Terminal colour themes for the browser shell (shell.js).
   The value after "window.MSB_SHELL_THEMES = " must stay strict JSON:
   tests/test_agent_circus_shell_themes.py parses it. Only colour values
   are taken from the named sources; no code is copied. */
window.MSB_SHELL_THEMES = [
  {
    "id": "msb-light", "label": "msb-circus light", "group": "Dashboard", "scheme": "light",
    "source": "dashboard.css (light)",
    "colors": {
      "background": "#fcfcfb", "foreground": "#0b0b0b", "cursor": "#2a78d6", "cursorAccent": "#fcfcfb",
      "selectionBackground": "#2a78d64d"
    }
  },
  {
    "id": "msb-dark", "label": "msb-circus dark", "group": "Dashboard", "scheme": "dark",
    "source": "dashboard.css (dark)",
    "colors": {
      "background": "#1a1a19", "foreground": "#f0efec", "cursor": "#3987e5", "cursorAccent": "#1a1a19",
      "selectionBackground": "#3987e54d"
    }
  },
  {
    "id": "modus-operandi", "label": "Modus Operandi", "group": "Modus", "scheme": "light",
    "source": "https://github.com/protesilaos/modus-themes (modus-themes.el 5.3.0)",
    "colors": {
      "background": "#ffffff", "foreground": "#000000", "cursor": "#000000", "cursorAccent": "#ffffff",
      "selectionBackground": "#bdbdbd", "black": "#000000", "red": "#a60000", "green": "#006800",
      "yellow": "#6f5500", "blue": "#0031a9", "magenta": "#721045", "cyan": "#005e8b",
      "white": "#a6a6a6", "brightBlack": "#595959", "brightRed": "#972500", "brightGreen": "#00663f",
      "brightYellow": "#884900", "brightBlue": "#3548cf", "brightMagenta": "#531ab6", "brightCyan": "#005f5f",
      "brightWhite": "#ffffff"
    }
  },
  {
    "id": "modus-operandi-tinted", "label": "Modus Operandi Tinted", "group": "Modus", "scheme": "light",
    "source": "https://github.com/protesilaos/modus-themes (modus-themes.el 5.3.0)",
    "colors": {
      "background": "#fbf7f0", "foreground": "#000000", "cursor": "#d00000", "cursorAccent": "#fbf7f0",
      "selectionBackground": "#c2bcb5", "black": "#000000", "red": "#a60000", "green": "#006300",
      "yellow": "#6d5000", "blue": "#0031a9", "magenta": "#721045", "cyan": "#00598b",
      "white": "#a6a6a6", "brightBlack": "#595959", "brightRed": "#972500", "brightGreen": "#00603f",
      "brightYellow": "#894000", "brightBlue": "#3546c2", "brightMagenta": "#531ab6", "brightCyan": "#005f5f",
      "brightWhite": "#ffffff"
    }
  },
  {
    "id": "modus-vivendi", "label": "Modus Vivendi", "group": "Modus", "scheme": "dark",
    "source": "https://github.com/protesilaos/modus-themes (modus-themes.el 5.3.0)",
    "colors": {
      "background": "#000000", "foreground": "#ffffff", "cursor": "#ffffff", "cursorAccent": "#000000",
      "selectionBackground": "#5a5a5a", "black": "#000000", "red": "#ff5f59", "green": "#44bc44",
      "yellow": "#d0bc00", "blue": "#2fafff", "magenta": "#feacd0", "cyan": "#00d3d0",
      "white": "#a6a6a6", "brightBlack": "#595959", "brightRed": "#ff6b55", "brightGreen": "#00c06f",
      "brightYellow": "#fec43f", "brightBlue": "#79a8ff", "brightMagenta": "#b6a0ff", "brightCyan": "#6ae4b9",
      "brightWhite": "#ffffff"
    }
  },
  {
    "id": "modus-vivendi-tinted", "label": "Modus Vivendi Tinted", "group": "Modus", "scheme": "dark",
    "source": "https://github.com/protesilaos/modus-themes (modus-themes.el 5.3.0)",
    "colors": {
      "background": "#0d0e1c", "foreground": "#ffffff", "cursor": "#ff66ff", "cursorAccent": "#0d0e1c",
      "selectionBackground": "#555a66", "black": "#000000", "red": "#ff5f59", "green": "#44bc44",
      "yellow": "#d0bc00", "blue": "#2fafff", "magenta": "#feacd0", "cyan": "#00d3d0",
      "white": "#a6a6a6", "brightBlack": "#595959", "brightRed": "#ff6b55", "brightGreen": "#11c777",
      "brightYellow": "#fec43f", "brightBlue": "#79a8ff", "brightMagenta": "#b6a0ff", "brightCyan": "#6ae4b9",
      "brightWhite": "#ffffff"
    }
  },
  {
    "id": "catppuccin-latte", "label": "Catppuccin Latte", "group": "Other", "scheme": "light",
    "source": "https://github.com/catppuccin/palette (palette.json 1.8.0)",
    "colors": {
      "background": "#eff1f5", "foreground": "#4c4f69", "cursor": "#dc8a78", "cursorAccent": "#eff1f5",
      "selectionBackground": "#acb0be", "black": "#5c5f77", "red": "#d20f39", "green": "#40a02b",
      "yellow": "#df8e1d", "blue": "#1e66f5", "magenta": "#ea76cb", "cyan": "#179299",
      "white": "#acb0be", "brightBlack": "#6c6f85", "brightRed": "#de293e", "brightGreen": "#49af3d",
      "brightYellow": "#eea02d", "brightBlue": "#456eff", "brightMagenta": "#fe85d8", "brightCyan": "#2d9fa8",
      "brightWhite": "#bcc0cc"
    }
  },
  {
    "id": "catppuccin-mocha", "label": "Catppuccin Mocha", "group": "Other", "scheme": "dark",
    "source": "https://github.com/catppuccin/palette (palette.json 1.8.0)",
    "colors": {
      "background": "#1e1e2e", "foreground": "#cdd6f4", "cursor": "#f5e0dc", "cursorAccent": "#1e1e2e",
      "selectionBackground": "#585b70", "black": "#45475a", "red": "#f38ba8", "green": "#a6e3a1",
      "yellow": "#f9e2af", "blue": "#89b4fa", "magenta": "#f5c2e7", "cyan": "#94e2d5",
      "white": "#a6adc8", "brightBlack": "#585b70", "brightRed": "#f37799", "brightGreen": "#89d88b",
      "brightYellow": "#ebd391", "brightBlue": "#74a8fc", "brightMagenta": "#f2aede", "brightCyan": "#6bd7ca",
      "brightWhite": "#bac2de"
    }
  },
  {
    "id": "dracula", "label": "Dracula", "group": "Other", "scheme": "dark",
    "source": "https://draculatheme.com/spec (dracula/alacritty)",
    "colors": {
      "background": "#282a36", "foreground": "#f8f8f2", "cursor": "#f8f8f2", "cursorAccent": "#282a36",
      "selectionBackground": "#44475a", "black": "#21222c", "red": "#ff5555", "green": "#50fa7b",
      "yellow": "#f1fa8c", "blue": "#bd93f9", "magenta": "#ff79c6", "cyan": "#8be9fd",
      "white": "#f8f8f2", "brightBlack": "#6272a4", "brightRed": "#ff6e6e", "brightGreen": "#69ff94",
      "brightYellow": "#ffffa5", "brightBlue": "#d6acff", "brightMagenta": "#ff92df", "brightCyan": "#a4ffff",
      "brightWhite": "#ffffff"
    }
  },
  {
    "id": "gruvbox-light", "label": "Gruvbox Light", "group": "Other", "scheme": "light",
    "source": "https://github.com/morhetz/gruvbox (alacritty-theme gruvbox_light)",
    "colors": {
      "background": "#fbf1c7", "foreground": "#3c3836", "cursor": "#3c3836", "cursorAccent": "#fbf1c7",
      "selectionBackground": "#bdae93", "black": "#fbf1c7", "red": "#cc241d", "green": "#98971a",
      "yellow": "#d79921", "blue": "#458588", "magenta": "#b16286", "cyan": "#689d6a",
      "white": "#7c6f64", "brightBlack": "#928374", "brightRed": "#9d0006", "brightGreen": "#79740e",
      "brightYellow": "#b57614", "brightBlue": "#076678", "brightMagenta": "#8f3f71", "brightCyan": "#427b58",
      "brightWhite": "#3c3836"
    }
  },
  {
    "id": "gruvbox-dark", "label": "Gruvbox Dark", "group": "Other", "scheme": "dark",
    "source": "https://github.com/morhetz/gruvbox (alacritty-theme gruvbox_dark)",
    "colors": {
      "background": "#282828", "foreground": "#ebdbb2", "cursor": "#ebdbb2", "cursorAccent": "#282828",
      "selectionBackground": "#665c54", "black": "#282828", "red": "#cc241d", "green": "#98971a",
      "yellow": "#d79921", "blue": "#458588", "magenta": "#b16286", "cyan": "#689d6a",
      "white": "#a89984", "brightBlack": "#928374", "brightRed": "#fb4934", "brightGreen": "#b8bb26",
      "brightYellow": "#fabd2f", "brightBlue": "#83a598", "brightMagenta": "#d3869b", "brightCyan": "#8ec07c",
      "brightWhite": "#ebdbb2"
    }
  },
  {
    "id": "nord", "label": "Nord", "group": "Other", "scheme": "dark",
    "source": "https://github.com/nordtheme/xresources, nordtheme/alacritty",
    "colors": {
      "background": "#2e3440", "foreground": "#d8dee9", "cursor": "#d8dee9", "cursorAccent": "#2e3440",
      "selectionBackground": "#4c566a", "black": "#3b4252", "red": "#bf616a", "green": "#a3be8c",
      "yellow": "#ebcb8b", "blue": "#81a1c1", "magenta": "#b48ead", "cyan": "#88c0d0",
      "white": "#e5e9f0", "brightBlack": "#4c566a", "brightRed": "#bf616a", "brightGreen": "#a3be8c",
      "brightYellow": "#ebcb8b", "brightBlue": "#81a1c1", "brightMagenta": "#b48ead", "brightCyan": "#8fbcbb",
      "brightWhite": "#eceff4"
    }
  },
  {
    "id": "solarized-light", "label": "Solarized Light", "group": "Other", "scheme": "light",
    "source": "https://github.com/altercation/solarized (README)",
    "colors": {
      "background": "#fdf6e3", "foreground": "#657b83", "cursor": "#586e75", "cursorAccent": "#fdf6e3",
      "selectionBackground": "#eee8d5", "black": "#073642", "red": "#dc322f", "green": "#859900",
      "yellow": "#b58900", "blue": "#268bd2", "magenta": "#d33682", "cyan": "#2aa198",
      "white": "#eee8d5", "brightBlack": "#002b36", "brightRed": "#cb4b16", "brightGreen": "#586e75",
      "brightYellow": "#657b83", "brightBlue": "#839496", "brightMagenta": "#6c71c4", "brightCyan": "#93a1a1",
      "brightWhite": "#fdf6e3"
    }
  },
  {
    "id": "solarized-dark", "label": "Solarized Dark", "group": "Other", "scheme": "dark",
    "source": "https://github.com/altercation/solarized (README)",
    "colors": {
      "background": "#002b36", "foreground": "#839496", "cursor": "#93a1a1", "cursorAccent": "#002b36",
      "selectionBackground": "#073642", "black": "#073642", "red": "#dc322f", "green": "#859900",
      "yellow": "#b58900", "blue": "#268bd2", "magenta": "#d33682", "cyan": "#2aa198",
      "white": "#eee8d5", "brightBlack": "#002b36", "brightRed": "#cb4b16", "brightGreen": "#586e75",
      "brightYellow": "#657b83", "brightBlue": "#839496", "brightMagenta": "#6c71c4", "brightCyan": "#93a1a1",
      "brightWhite": "#fdf6e3"
    }
  }
];
