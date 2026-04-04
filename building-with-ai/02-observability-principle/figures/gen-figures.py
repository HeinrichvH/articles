"""
Generate figures for Article 02: Observability >> Predictability.

Figure 1: State space explosion — services vs possible states (log scale)
Figure 2: Graph inside the graph — fractal service complexity (Online Boutique)

Usage:
    python gen-figures.py
"""

import matplotlib.pyplot as plt
import numpy as np
from pathlib import Path

HERE = Path(__file__).parent

# Consistent style matching article 01 figures
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
BG = '#1a1a2e'


# ─────────────────────────────────────────────────────────────
# Figure 1: State space explosion
# ─────────────────────────────────────────────────────────────

def draw_state_space_explosion():
    fig, ax = plt.subplots(figsize=(10, 5.5))

    n = np.arange(1, 16)
    s = 10  # states per service
    state_space = s ** n

    # State space curve (exponential)
    ax.semilogy(n, state_space, color=RED, lw=3, zorder=5, marker='o',
                markersize=7, markerfacecolor=RED, markeredgecolor=BG,
                markeredgewidth=2, label='Possible system states ($s^n$, s=10)')

    # Test suite coverage line (flat)
    test_coverage = 500
    ax.axhline(y=test_coverage, color=TEAL, lw=2.5, linestyle='--', zorder=4,
               label=f'Test suite coverage (~{test_coverage} scenarios)')

    # Fill the gap
    ax.fill_between(n, test_coverage, state_space,
                    where=(state_space > test_coverage),
                    alpha=0.1, color=RED)

    # Annotate the gap — moved to upper-left of shaded region
    ax.annotate('Untested state space\n(grows exponentially)',
                xy=(4, 1e12), fontsize=12, fontweight='bold',
                color=RED, ha='center', alpha=0.8)

    # Annotate specific points
    ax.annotate(f'7 services\n{s**7:,} states',
                xy=(7, s**7), xytext=(3.5, s**9),
                fontsize=10, color=GOLD,
                arrowprops=dict(arrowstyle='->', color=GOLD, lw=1.5))

    ax.annotate(f'10 services\n{s**10:,.0f} states',
                xy=(10, s**10), xytext=(11.5, s**7),
                fontsize=10, color=GOLD,
                arrowprops=dict(arrowstyle='->', color=GOLD, lw=1.5))

    ax.set_xlabel('Number of services (n)', fontsize=13, labelpad=10)
    ax.set_ylabel('Possible system states (log scale)', fontsize=13, labelpad=10)
    ax.set_xticks(n)
    ax.set_xlim(0.5, 15.5)
    ax.set_ylim(1, 1e16)

    ax.legend(loc='upper left', fontsize=11, framealpha=0.3,
              edgecolor='#333355')

    ax.grid(True, alpha=0.1, color='#ffffff', which='both')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color('#333355')
    ax.spines['bottom'].set_color('#333355')

    plt.tight_layout()
    out = HERE / 'article-state-space.png'
    plt.savefig(out, dpi=200, bbox_inches='tight', facecolor=BG)
    plt.close()
    print(f'Saved: {out}')


# ─────────────────────────────────────────────────────────────
# Figure 2: Graph inside the graph — Online Boutique
# ─────────────────────────────────────────────────────────────

def draw_graph_in_graph():
    fig, ax = plt.subplots(figsize=(12, 6))

    # === Outer graph: Online Boutique service topology ===
    outer_nodes = {
        'Frontend':          (0.10, 0.75),
        'Cart\nService':     (0.30, 0.92),
        'Checkout\nService': (0.35, 0.65),
        'Product\nCatalog':  (0.30, 0.42),
        'Payment\nService':  (0.62, 0.90),
        'Shipping\nService': (0.62, 0.42),
        'Rec.\nService': (0.62, 0.66),
    }

    outer_edges = [
        ('Frontend', 'Cart\nService'),
        ('Frontend', 'Checkout\nService'),
        ('Frontend', 'Product\nCatalog'),
        ('Frontend', 'Rec.\nService'),
        ('Checkout\nService', 'Payment\nService'),
        ('Checkout\nService', 'Shipping\nService'),
        ('Checkout\nService', 'Cart\nService'),
        ('Checkout\nService', 'Product\nCatalog'),
        ('Rec.\nService', 'Product\nCatalog'),
    ]

    # Draw outer edges
    for u, v in outer_edges:
        ux, uy = outer_nodes[u]
        vx, vy = outer_nodes[v]
        ax.annotate('', xy=(vx, vy), xytext=(ux, uy),
                    arrowprops=dict(arrowstyle='->', color=TEAL + '88',
                                    lw=1.5, connectionstyle='arc3,rad=0.08'))

    # Draw outer nodes
    highlight = 'Checkout\nService'
    for name, (x, y) in outer_nodes.items():
        if name == highlight:
            circle = plt.Circle((x, y), 0.055, facecolor='#0f3460',
                               edgecolor=GOLD, lw=2.5, zorder=5)
            ax.add_patch(circle)
            ax.text(x, y, name, ha='center', va='center', fontsize=7.5,
                    fontweight='bold', color=GOLD, zorder=6)
        else:
            circle = plt.Circle((x, y), 0.042, facecolor='#2a2a4a',
                               edgecolor=TEAL, lw=1.5, zorder=5)
            ax.add_patch(circle)
            ax.text(x, y, name, ha='center', va='center', fontsize=6.5,
                    color='#e0e0e0', zorder=6)

    # === Zoom callout: internal graph of CheckoutService ===
    hl_x, hl_y = outer_nodes[highlight]

    # Detail box
    box_x, box_y, box_w, box_h = 0.05, 0.02, 0.62, 0.32
    detail_box = plt.Rectangle((box_x, box_y), box_w, box_h,
                                fill=True, facecolor='#0f3460',
                                edgecolor=GOLD, lw=2, linestyle='-',
                                zorder=3, alpha=0.9)
    ax.add_patch(detail_box)

    # Zoom connector lines
    ax.plot([hl_x - 0.03, box_x], [hl_y - 0.055, box_y + box_h],
            color=GOLD, lw=1, linestyle='--', zorder=2)
    ax.plot([hl_x + 0.03, box_x + box_w], [hl_y - 0.055, box_y + box_h],
            color=GOLD, lw=1, linestyle='--', zorder=2)

    # Internal nodes of CheckoutService — 2 rows of 3
    cx = box_x + box_w / 2  # center x of box
    row1_y = box_y + box_h * 0.68
    row2_y = box_y + box_h * 0.28
    col_spacing = box_w / 3.5

    internal_nodes = {
        'gRPC\nHandler':   (cx - col_spacing, row1_y),
        'Order\nLogic':    (cx, row1_y),
        'Payment\nClient': (cx + col_spacing, row1_y),
        'Shipping\nClient': (cx - col_spacing, row2_y),
        'Cart\nClient':    (cx, row2_y),
        'DB\nConnection':  (cx + col_spacing, row2_y),
    }

    internal_edges = [
        ('gRPC\nHandler', 'Order\nLogic'),
        ('Order\nLogic', 'Payment\nClient'),
        ('Order\nLogic', 'Shipping\nClient'),
        ('Order\nLogic', 'Cart\nClient'),
        ('Order\nLogic', 'DB\nConnection'),
    ]

    # Draw internal edges
    for u, v in internal_edges:
        ux, uy = internal_nodes[u]
        vx, vy = internal_nodes[v]
        ax.annotate('', xy=(vx, vy), xytext=(ux, uy),
                    arrowprops=dict(arrowstyle='->', color=PURPLE + '88',
                                    lw=1.2, connectionstyle='arc3,rad=0.08'))

    # Draw internal nodes
    node_r = 0.032
    for name, (x, y) in internal_nodes.items():
        circle = plt.Circle((x, y), node_r, facecolor=BG,
                           edgecolor=PURPLE, lw=1.5, zorder=5)
        ax.add_patch(circle)
        ax.text(x, y, name, ha='center', va='center', fontsize=5.5,
                color=PURPLE, zorder=6, fontweight='bold')

    # Labels
    ax.text(cx, box_y + box_h + 0.02, 'Internal graph of CheckoutService',
            ha='center', fontsize=9, color=GOLD, style='italic')

    ax.set_xlim(0.0, 0.78)
    ax.set_ylim(-0.02, 1.02)
    ax.set_aspect('equal')
    ax.axis('off')

    plt.tight_layout()
    out = HERE / 'article-graph-in-graph.png'
    plt.savefig(out, dpi=200, bbox_inches='tight', facecolor=BG)
    plt.close()
    print(f'Saved: {out}')


if __name__ == '__main__':
    draw_state_space_explosion()
    draw_graph_in_graph()
