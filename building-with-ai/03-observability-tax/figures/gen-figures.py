"""
Generate figures for Article 03: The Observability Tax.

Figure 1: Graph comparison — full observability (Dijkstra) vs partial
          observability (exploration tree).
Figure 2: Competitive ratio — 2^k + 1 vs linear intuition.

Usage:
    python gen-figures.py
"""

import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path

HERE = Path(__file__).parent

# Palette matches articles 01 & 02
plt.rcParams.update({
    'figure.facecolor': '#1a1a2e',
    'axes.facecolor': '#1a1a2e',
    'text.color': '#e0e0e0',
    'axes.labelcolor': '#e0e0e0',
    'xtick.color': '#e0e0e0',
    'ytick.color': '#e0e0e0',
    'font.family': 'sans-serif',
    'font.size': 11,
})

TEAL = '#4ecdc4'
RED = '#ff6b6b'
GOLD = '#ffd93d'
PURPLE = '#b388ff'
GRAY = '#555566'
BG = '#1a1a2e'


# ─────────────────────────────────────────────────────────────
# Figure 1: Graph comparison — full vs partial observability
# ─────────────────────────────────────────────────────────────

def draw_graph_comparison():
    fig, (ax_full, ax_partial) = plt.subplots(
        1, 2, figsize=(14, 6.5), gridspec_kw={'wspace': 0.08})

    # Shared topology: 7 nodes, simple DAG
    nodes = {
        'A': (0.08, 0.50),
        'B': (0.30, 0.78),
        'C': (0.30, 0.22),
        'D': (0.55, 0.90),
        'E': (0.55, 0.50),
        'F': (0.55, 0.10),
        'G': (0.88, 0.50),
    }
    edges = [
        ('A', 'B'), ('A', 'C'),
        ('B', 'D'), ('B', 'E'),
        ('C', 'E'), ('C', 'F'),
        ('D', 'G'), ('E', 'G'), ('F', 'G'),
    ]
    optimal_path = [('A', 'B'), ('B', 'E'), ('E', 'G')]

    def draw_panel(ax, hidden=None, title='', subtitle='', mode='full'):
        hidden = hidden or set()

        # Edges first (underneath nodes)
        for u, v in edges:
            ux, uy = nodes[u]
            vx, vy = nodes[v]
            is_optimal = (u, v) in optimal_path or (v, u) in optimal_path
            touches_hidden = u in hidden or v in hidden

            if mode == 'full' and is_optimal:
                color, lw, alpha = TEAL, 3.5, 1.0
            elif mode == 'partial' and touches_hidden:
                color, lw, alpha = GRAY, 1.5, 0.4
            else:
                color, lw, alpha = TEAL + '66', 1.5, 0.6

            ax.annotate('', xy=(vx, vy), xytext=(ux, uy),
                        arrowprops=dict(arrowstyle='->', color=color,
                                        lw=lw, alpha=alpha,
                                        connectionstyle='arc3,rad=0.05'))

        # Exploration arrows for partial panel — fan out from A through
        # candidate paths (everything branching off the hidden node)
        if mode == 'partial':
            explore_paths = [
                [('A', 'B'), ('B', 'D'), ('D', 'G')],
                [('A', 'B'), ('B', 'E'), ('E', 'G')],
                [('A', 'C'), ('C', 'E'), ('E', 'G')],
                [('A', 'C'), ('C', 'F'), ('F', 'G')],
            ]
            for path in explore_paths:
                for u, v in path:
                    ux, uy = nodes[u]
                    vx, vy = nodes[v]
                    ax.annotate('', xy=(vx, vy), xytext=(ux, uy),
                                arrowprops=dict(arrowstyle='->', color=RED,
                                                lw=1.2, alpha=0.35,
                                                connectionstyle='arc3,rad=0.18'))

        # Nodes
        for name, (x, y) in nodes.items():
            is_hidden = name in hidden
            is_endpoint = name in ('A', 'G')

            if is_hidden:
                face, edge, text_color = BG, GRAY, GRAY
                lw = 1.5
                linestyle = '--'
            elif is_endpoint:
                face, edge, text_color = '#0f3460', GOLD, GOLD
                lw = 2.5
                linestyle = '-'
            else:
                face, edge, text_color = '#2a2a4a', TEAL, '#e0e0e0'
                lw = 2.0
                linestyle = '-'

            circle = plt.Circle((x, y), 0.055, facecolor=face,
                                edgecolor=edge, lw=lw, ls=linestyle, zorder=5)
            ax.add_patch(circle)
            ax.text(x, y, name, ha='center', va='center',
                    fontsize=12, fontweight='bold',
                    color=text_color, zorder=6)

            if is_hidden:
                ax.text(x, y - 0.11, '(hidden)', ha='center', va='center',
                        fontsize=8, color=GRAY, style='italic', zorder=6)

        # Title & subtitle
        ax.text(0.5, 1.08, title, transform=ax.transAxes,
                ha='center', va='bottom', fontsize=15, fontweight='bold',
                color=GOLD if mode == 'full' else RED)
        ax.text(0.5, 1.02, subtitle, transform=ax.transAxes,
                ha='center', va='bottom', fontsize=11,
                color='#a0a0b0', style='italic')

        ax.set_xlim(-0.02, 1.02)
        ax.set_ylim(-0.08, 1.02)
        ax.set_aspect('equal')
        ax.axis('off')

    draw_panel(
        ax_full, hidden=None, mode='full',
        title='Full observability',
        subtitle='Dijkstra finds the optimal path · O(E + V log V)')

    draw_panel(
        ax_partial, hidden={'B', 'E'}, mode='partial',
        title='Partial observability',
        subtitle='Exploration across every candidate · PSPACE-complete')

    plt.tight_layout()
    out = HERE / 'article-graph-comparison.png'
    plt.savefig(out, dpi=200, bbox_inches='tight', facecolor=BG)
    plt.close()
    print(f'Saved: {out}')


# ─────────────────────────────────────────────────────────────
# Figure 2: Competitive ratio — 2k+1 vs k+1 (both linear)
# ─────────────────────────────────────────────────────────────

def draw_competitive_ratio():
    fig, ax = plt.subplots(figsize=(11, 6.5))

    k = np.arange(0, 11)
    cr_correct = 2.0 * k + 1   # Bar-Noy & Schieber (1991): 2k+1
    cr_lin = k + 1              # naive intuition: k+1

    # Correct competitive ratio (2k+1)
    ax.plot(k, cr_correct, color=RED, lw=3, zorder=5,
            marker='o', markersize=9,
            markerfacecolor=RED, markeredgecolor=BG, markeredgewidth=2,
            label=r'Competitive ratio (Bar-Noy & Schieber 1991):  $2k + 1$')

    # Naive intuition
    ax.plot(k, cr_lin, color=TEAL, lw=2, linestyle='--', zorder=4,
            label=r'Naive intuition:  $k + 1$')

    # Fill the gap between them to show the tax
    ax.fill_between(k, cr_lin, cr_correct, where=(cr_correct > cr_lin),
                    alpha=0.1, color=RED)

    # 10× reference line
    ax.axhline(y=10, color='#333355', lw=1, linestyle=':', zorder=2)
    ax.text(10.3, 10, '10×', fontsize=10, color='#888899',
            va='center', ha='left')

    # Annotate table points from the article
    table_points = [
        (0,  1,  '0 hidden\n1×'),
        (1,  3,  '1 hidden\n3×'),
        (3,  7,  '3 hidden\n7×'),
        (5,  11, '5 hidden\n11×'),
        (7,  15, '7 hidden\n15×'),
        (10, 21, '10 hidden\n21×'),
    ]
    offsets = {0: (-1.2, 1.5), 1: (0.5, 1.5), 3: (0.5, 1.5),
               5: (-2.5, 1.5), 7: (0.5, 1.5), 10: (-2.5, -2)}
    for kx, cry, lbl in table_points:
        dx, dy = offsets.get(kx, (0.5, 1.5))
        ax.annotate(lbl, xy=(kx, cry), xytext=(kx + dx, cry + dy),
                    fontsize=9, color=GOLD,
                    arrowprops=dict(arrowstyle='->', color=GOLD, lw=1.2,
                                    alpha=0.8))

    ax.set_xlabel('Hidden subsystems (k)', fontsize=13, labelpad=10)
    ax.set_ylabel('Competitive ratio', fontsize=13, labelpad=10)
    ax.set_xticks(k)
    ax.set_xlim(-0.3, 11)
    ax.set_ylim(0, 26)

    ax.legend(loc='upper left', fontsize=11, framealpha=0.3,
              edgecolor='#333355')

    ax.grid(True, alpha=0.1, color='#ffffff')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#333355')
    ax.spines['bottom'].set_color('#333355')

    ax.text(0.5, -0.18,
            'Each additional dark subsystem adds 2× to worst-case debugging cost '
            '(Bar-Noy & Schieber, 1991).',
            transform=ax.transAxes, ha='center', fontsize=11,
            style='italic', color='#a0a0b0')

    plt.tight_layout()
    out = HERE / 'article-competitive-ratio.png'
    plt.savefig(out, dpi=200, bbox_inches='tight', facecolor=BG)
    plt.close()
    print(f'Saved: {out}')


if __name__ == '__main__':
    draw_graph_comparison()
    draw_competitive_ratio()
