"""Cross-Template Uniqueness and Vector Similarity Distribution Test."""
import json
from pathlib import Path
import numpy as np
import pytest

from mcp_server import rag_engine

try:
    import faiss
except ImportError:
    faiss = None


def get_template_vectors_and_dataset():
    dataset = rag_engine.load_dataset()
    assert len(dataset) > 0, "Template dataset must not be empty"

    index = rag_engine.get_faiss_index()
    assert index is not None, "FAISS index must be available"
    assert index.ntotal == len(dataset), f"Index vector count ({index.ntotal}) must match dataset ({len(dataset)})"

    vectors = np.empty((index.ntotal, index.d), dtype=np.float32)
    for i in range(index.ntotal):
        vectors[i] = index.reconstruct(i)

    return dataset, vectors


def test_cross_template_pairwise_similarity_distribution():
    """
    Computes pairwise cross-similarity across all template combinations (N * (N-1) / 2)
    and validates distribution in 0.05 intervals.
    """
    dataset, vectors = get_template_vectors_and_dataset()
    N = len(dataset)
    expected_pairs = N * (N - 1) // 2

    # Compute cosine similarity matrix (vectors are L2-normalized)
    sim_matrix = vectors @ vectors.T

    # Extract unique upper triangular pairs
    triu_indices = np.triu_indices(N, k=1)
    pair_similarities = sim_matrix[triu_indices]

    assert len(pair_similarities) == expected_pairs
    assert pair_similarities.min() >= -1.0
    assert pair_similarities.max() <= 1.0

    # Ensure no exact duplicate distinct templates (similarity < 0.999)
    assert pair_similarities.max() < 0.999, f"Found near identical duplicate template pair with similarity {pair_similarities.max()}"

    # 0.05 Interval Bins [0.00, 0.05, 0.10, ..., 1.00]
    bins = np.arange(0.0, 1.05, 0.05)
    hist, bin_edges = np.histogram(pair_similarities, bins=bins)

    print(f"\n=================================================================")
    print(f"📊 CROSS-TEMPLATE UNIQUENESS & SIMILARITY DISTRIBUTION REPORT")
    print(f"=================================================================")
    print(f"• Toplam Şablon Sayısı (Dataset Size)       : {N}")
    print(f"• Değerlendirilen Benzersiz Çift (Pairs)    : {len(pair_similarities):,}")
    print(f"• Min Benzerlik (En Benzersiz Çift)        : {pair_similarities.min():.4f}")
    print(f"• Max Benzerlik (En Çok Benzeyen Çift)     : {pair_similarities.max():.4f}")
    print(f"• Ortalama Benzerlik (Mean Similarity)      : {pair_similarities.mean():.4f}")
    print(f"• Medyan Benzerlik (Median)                 : {np.median(pair_similarities):.4f}")
    print(f"• Standart Sapma (Std Dev)                  : {pair_similarities.std():.4f}")
    print(f"=================================================================")
    print(f"{'0.05 ARALIK (Cosine Sim)':<26} | {'ÇİFT SAYISI':<12} | {'ORAN (%)':<10} | GÖRSEL DAĞILIM")
    print(f"-----------------------------------------------------------------")

    for i in range(len(hist)):
        lower = bin_edges[i]
        upper = bin_edges[i+1]
        count = hist[i]
        pct = (count / len(pair_similarities)) * 100
        bar = "█" * int(pct / 1.5)
        print(f"[{lower:.2f} - {upper:.2f}){' '*10:<11} | {count:<12} | {pct:>6.2f}%    | {bar}")

    # Top 5 most similar pairs (highest cross-similarity / lowest uniqueness)
    top_indices = np.argsort(pair_similarities)[-5:][::-1]
    rows, cols = triu_indices
    print(f"\n-----------------------------------------------------------------")
    print(f"🔥 EN YÜKSEK BENZERLİK GÖSTEREN 5 ŞABLON ÇİFTİ (En Düşük Uniqueness):")
    print(f"-----------------------------------------------------------------")
    for rank, idx in enumerate(top_indices, 1):
        r, c = rows[idx], cols[idx]
        print(f"{rank}. Benzerlik Skoru: {pair_similarities[idx]:.4f}")
        print(f"   A: {dataset[r].get('title')} (ID: {dataset[r].get('id')})")
        print(f"   B: {dataset[c].get('title')} (ID: {dataset[c].get('id')})")

    # Top 5 most distinct pairs (lowest cross-similarity / highest uniqueness)
    bot_indices = np.argsort(pair_similarities)[:5]
    print(f"\n-----------------------------------------------------------------")
    print(f"💎 EN DÜŞÜK BENZERLİK GÖSTEREN 5 ŞABLON ÇİFTİ (En Yüksek Uniqueness):")
    print(f"-----------------------------------------------------------------")
    for rank, idx in enumerate(bot_indices, 1):
        r, c = rows[idx], cols[idx]
        print(f"{rank}. Benzerlik Skoru: {pair_similarities[idx]:.4f}")
        print(f"   A: {dataset[r].get('title')} (ID: {dataset[r].get('id')})")
        print(f"   B: {dataset[c].get('title')} (ID: {dataset[c].get('id')})")
    print(f"=================================================================\n")
