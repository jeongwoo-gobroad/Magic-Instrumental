# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path


def stylesheet(base, mode):
    if mode == "light":
        for dark, light in {
            "#11151d": "#f3f5f7",
            "#e7edf7": "#202b3b",
            "#99a9bf": "#526078",
            "#19212d": "#ffffff",
            "#242e3e": "#ffffff",
            "#35425a": "#b7c4d3",
            "#2c384b": "#c8d0dc",
            "#1b2330": "#e5e9ef",
            "#304b50": "#d4eee7",
            "#a2f5d9": "#114d3b",
            "#87ddc4": "#12624e",
            "#2b384b": "#ccd5df",
        }.items():
            base = base.replace(dark, light)
    icon = (Path(__file__).parent / "assets/check.svg").as_posix()
    return (
        base
        + f"""
QCheckBox {{ spacing: 8px; padding: 4px 0; }}
QCheckBox::indicator {{ width: 21px; height: 21px; border: 2px solid #7c8ea8;
    border-radius: 5px; background: transparent; }}
QCheckBox::indicator:checked {{ background: #8cddc5; border-color: #438e78;
    image: url("{icon}"); }}
QCheckBox::indicator:disabled {{ border-color: #697586; }}
QFrame#separator {{ background: #7c8ea8; max-height: 1px; margin: 9px 0; }}
QSpinBox, QDoubleSpinBox, QLineEdit {{ border: 1px solid #7c8ea8; border-radius: 5px;
    padding: 5px; min-height: 20px; }}
"""
    )
