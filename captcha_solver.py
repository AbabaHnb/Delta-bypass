#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 图形验证码打码器

import sys, io, time, json, argparse
import numpy as np
from PIL import Image
from scipy import ndimage
from scipy.spatial import cKDTree
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import numba
from numba import njit, prange
from fake_useragent import UserAgent

_ua = UserAgent(platforms='mobile')

# fake_useragent 的 .random 每次约 16ms，高并发下是显著开销：启动时预生成后轮转取用
import itertools as _it
_UA_POOL = []
_UA_IDX = _it.count()


def _rand_ua():
    global _UA_POOL
    if not _UA_POOL:
        seen = []
        for _ in range(32):
            try:
                seen.append(_ua.random)
            except Exception:
                break
        _UA_POOL = list(dict.fromkeys(seen)) or ['Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)']
    return _UA_POOL[next(_UA_IDX) % len(_UA_POOL)]

API = "https://captcha.platorelay.com/api"
_V8_DEBUG = {}

def _to_gray(f):
    # 与 f.mean(axis=2) 数值等价(float32, 偏差<1e-5)，但快 ~6x：
    # uint8 三通道先整数相加再一次除法，避免 float64 逐通道均值。
    return (f[:, :, 0].astype(np.uint16) + f[:, :, 1] + f[:, :, 2]) / np.float32(3)


def _gray_frames(fr):
    return [_to_gray(f) for f in fr]


def _median_bg(grays):
    # 等价 np.median(np.stack(grays), axis=0)，用 partition 取中位，快 ~3x
    st = np.stack(grays)
    n = st.shape[0]
    k = n // 2
    part = np.partition(st, k, axis=0)
    if n % 2:
        return part[k]
    return (part[k - 1] + part[k]) / np.float32(2)


def session():
    s = requests.Session()
    _ad = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=32, max_retries=0)
    s.mount('https://', _ad)
    s.mount('http://', _ad)
    s.headers.update({'User-Agent': _rand_ua(), 'Referer': 'https://captcha.platorelay.com/'})
    return s

# numba 加速

@njit(cache=True)
def _label_centroids(mask, min_area=3):
    # 连通域标记 质心计算
    H, W = mask.shape
    #连通域标记
    labels = np.zeros((H, W), np.int32)
    eq = np.zeros(4096, np.int32)
    next_label = 1
    for y in range(H):
        for x in range(W):
            if mask[y, x]:
                up = labels[y - 1, x] if y > 0 else 0
                left = labels[y, x - 1] if x > 0 else 0
                if up == 0 and left == 0:
                    labels[y, x] = next_label
                    next_label += 1
                elif up == 0:
                    labels[y, x] = left
                elif left == 0:
                    labels[y, x] = up
                elif up == left:
                    labels[y, x] = up
                else:
                    mn = up if up < left else left
                    mx = up if up > left else left
                    labels[y, x] = mn
                    eq[mx] = mn

    #合并等价标签
    for y in range(H):
        for x in range(W):
            l = labels[y, x]
            if l > 0:
                while eq[l] != 0:
                    l = eq[l]
                labels[y, x] = l

    #统计每个标签的像素数
    n_labels = next_label - 1
    if n_labels == 0:
        return np.empty((0, 2), np.float64)
    sums = np.zeros((n_labels + 1, 3), np.float64)
    for y in range(H):
        for x in range(W):
            l = labels[y, x]
            if l > 0:
                sums[l, 0] += x
                sums[l, 1] += y
                sums[l, 2] += 1
    out = []
    for l in range(1, n_labels + 1):
        if sums[l, 2] >= min_area:
            out.append((sums[l, 0] / sums[l, 2], sums[l, 1] / sums[l, 2]))
    if not out:
        return np.empty((0, 2), np.float64)
    return np.array(out, np.float64)

def blobs(mask, min_area=3):
    #numba加速
    return _label_centroids(mask, min_area)

@njit(cache=True)
def _label_full(mask, min_area=3):
    #连通域标记
    H, W = mask.shape
    labels = np.zeros((H, W), np.int32)
    eq = np.zeros(4096, np.int32)
    next_label = 1
    for y in range(H):
        for x in range(W):
            if mask[y, x]:
                up = labels[y - 1, x] if y > 0 else 0
                left = labels[y, x - 1] if x > 0 else 0
                if up == 0 and left == 0:
                    labels[y, x] = next_label
                    next_label += 1
                elif up == 0:
                    labels[y, x] = left
                elif left == 0:
                    labels[y, x] = up
                elif up == left:
                    labels[y, x] = up
                else:
                    mn = up if up < left else left
                    mx = up if up > left else left
                    labels[y, x] = mn
                    eq[mx] = mn
    for y in range(H):
        for x in range(W):
            l = labels[y, x]
            if l > 0:
                while eq[l] != 0:
                    l = eq[l]
                labels[y, x] = l
    n_labels = next_label - 1
    if n_labels == 0:
        return labels, np.empty((0, 2), np.float64), np.zeros(0, np.int32)
    cnt = np.zeros(n_labels + 1, np.int32)
    sums = np.zeros((n_labels + 1, 2), np.float64)
    for y in range(H):
        for x in range(W):
            l = labels[y, x]
            if l > 0:
                cnt[l] += 1
                sums[l, 0] += x
                sums[l, 1] += y
    cents_out = []
    sizes_out = []
    for l in range(1, n_labels + 1):
        if cnt[l] >= min_area:
            cents_out.append((sums[l, 0] / cnt[l], sums[l, 1] / cnt[l]))
            sizes_out.append(cnt[l])
    if not cents_out:
        return labels, np.empty((0, 2), np.float64), np.zeros(0, np.int32)
    return labels, np.array(cents_out, np.float64), np.array(sizes_out, np.int32)

def load_gif(img_bytes, step=1):
    im = Image.open(io.BytesIO(img_bytes))
    fr = []
    n = im.n_frames
    for i in range(0, n, step):
        try:
            im.seek(i)
        except Exception:
            break
        fr.append(np.asarray(im.convert('RGB'), np.uint8))
    return fr

def dark_mask(fr_t, mode='lum'):
    if mode == 'rgb':
        return (fr_t[..., 0] < 150) & (fr_t[..., 1] < 150) & (fr_t[..., 2] < 150)
    return _to_gray(fr_t) < 170

def point_centers(fr_t, mode='rgb'):
    return blobs(dark_mask(fr_t, mode), min_area=3)

def _fit_circle(pts):
    n = len(pts)
    A = np.column_stack([2*pts[:,0], 2*pts[:,1], np.ones(n)])
    b = pts[:,0]**2 + pts[:,1]**2
    cx, cy, _ = np.linalg.lstsq(A, b, rcond=None)[0]
    r = float(np.median(np.hypot(pts[:,0]-cx, pts[:,1]-cy)))
    return cx, cy, r

def _track_at_threshold(fr, th, min_area=10, match_r=35, grays=None):
    H, W = fr[0].shape[:2]
    if grays is None:
        grays = _gray_frames(fr)
    fcent = []
    for g in grays:
        m = g < th
        lab, n = ndimage.label(m)
        if n == 0:
            fcent.append(np.empty((0,2))); continue
        sizes = np.array(ndimage.sum(m, lab, range(1, n+1)))
        sel = np.where(sizes >= min_area)[0] + 1
        if len(sel) == 0:
            fcent.append(np.empty((0,2))); continue
        cents = ndimage.center_of_mass(m, lab, sel)
        fcent.append(np.array([[c[1], c[0]] for c in cents], float))

    m0 = grays[0] < th
    lab0, n0 = ndimage.label(m0)
    sizes0 = np.array(ndimage.sum(m0, lab0, range(1, n0+1)))

    results = []
    for idx in np.where(sizes0 >= min_area)[0] + 1:
        yy, xx = np.where(lab0 == idx)
        sx, sy = xx.mean(), yy.mean()
        if sx < 15 or sx > W-15 or sy < 15 or sy > H-15:
            continue
        area0 = sizes0[idx-1]
        bbox_area = (xx.max()-xx.min()+1) * (yy.max()-yy.min()+1)
        compact = area0 / max(1, bbox_area)

        cur = np.array([sx, sy])
        track = [cur.copy()]
        for t in range(1, len(fcent)):
            P = fcent[t]
            if len(P) == 0:
                track.append(None); continue
            d = np.hypot(P[:,0]-cur[0], P[:,1]-cur[1])
            k = int(np.argmin(d))
            if d[k] < match_r:
                cur = P[k]; track.append(cur.copy())
            else:
                track.append(None)
        tp = [p for p in track if p is not None]
        if len(tp) < 6:
            continue

        pts = np.array(tp)
        pcx, pcy = pts.mean(axis=0)
        try:
            fcx, fcy, r = _fit_circle(pts)
        except:
            fcx, fcy, r = pcx, pcy, 0.0

        if 5 < r < 250:
            cx, cy = fcx, fcy
            is_circle = True
        else:
            cx, cy = pcx, pcy
            is_circle = False

        ang = np.unwrap(np.arctan2(pts[:,1]-cy, pts[:,0]-cx))
        da = np.diff(ang)
        total_disp = float(ang[-1] - ang[0])
        med = float(np.median(da))
        mad = float(np.median(np.abs(da-med))) + 1e-6
        good = np.abs(da-med) < 3.5*mad
        omega = float(np.median(da[good])) if good.sum() >= 3 else med
        rr = np.hypot(pts[:,0]-cx, pts[:,1]-cy)
        rad_err = float(np.median(np.abs(rr-np.median(rr))) / (np.median(rr)+1e-6))
        cons = float(abs(np.mean(np.sign(da))))
        conf = cons / (1.0 + 10.0*rad_err)

        results.append({'cx': sx, 'cy': sy, 'omega': omega, 'conf': conf,
                       'cons': cons, 'rad': rad_err, 'r': r,
                       'total_disp': total_disp,
                       'type': 'circle' if is_circle else 'noncircle',
                       'area': area0, 'compact': compact, 'n': len(tp), 'th': th})
    return results

def _region_centroid_rotation(fr, cx0, cy0, half=35, th=170, grays=None):
    H, W = fr[0].shape[:2]
    if grays is None:
        grays = _gray_frames(fr)
    centroids = []
    for t in range(len(grays)):
        x0, x1 = max(0, int(cx0)-half), min(W, int(cx0)+half)
        y0, y1 = max(0, int(cy0)-half), min(H, int(cy0)+half)
        region = grays[t][y0:y1, x0:x1]
        m = region < th
        if m.sum() < 10:
            centroids.append(None)
            continue
        yy, xx = np.where(m)
        centroids.append((xx.mean()+x0, yy.mean()+y0))

    pts = np.array([c for c in centroids if c is not None])
    if len(pts) < 6:
        return 0.0
    ccx, ccy = pts.mean(axis=0)
    ang = np.unwrap(np.arctan2(pts[:,1]-ccy, pts[:,0]-ccx))
    return float(ang[-1] - ang[0])


def _grid_angular_momentum(fr, grid=6, th=170, grays=None):
    #角动量扫描
    H, W = fr[0].shape[:2]
    if grays is None:
        grays = _gray_frames(fr)
    n = len(grays)
    bh, bw = H // grid, W // grid

    ang_mom = np.zeros((grid, grid), float)
    pixel_count = np.zeros((grid, grid), int)

    for t in range(n - 1):
        m = grays[t] < th
        yy, xx = np.where(m)
        if len(yy) < 10:
            continue
        m2 = grays[t+1] < th
        yy2, xx2 = np.where(m2)
        if len(yy2) < 10:
            continue

        tree2 = cKDTree(np.column_stack([xx2, yy2]))
        d, idx = tree2.query(np.column_stack([xx, yy]), k=1)
        moved = d > 1.5
        if moved.sum() < 5:
            continue

        px, py = xx[moved], yy[moved]
        nx, ny = xx2[idx[moved]], yy2[idx[moved]]
        dx, dy = nx - px, ny - py

        bi = np.clip(px // bw, 0, grid-1)
        bj = np.clip(py // bh, 0, grid-1)
        bcx = bi * bw + bw / 2.0
        bcy = bj * bh + bh / 2.0
        L = (px - bcx) * dy - (py - bcy) * dx

        np.add.at(ang_mom, (bj, bi), L)
        np.add.at(pixel_count, (bj, bi), 1)

    with np.errstate(divide='ignore', invalid='ignore'):
        norm_mom = np.where(pixel_count > 5, ang_mom / pixel_count, 0.0)
    return norm_mom, pixel_count


def _grid_find_opposite_from_mom(norm_mom, pixel_count, major_sign, fr, grays, grid=6, th=170):
    #角动量里找反向旋转最强的块
    H, W = fr[0].shape[:2]
    bh, bw = H // grid, W // grid

    best_val, best_cell = 0, None
    for j in range(1, grid-1):
        for i in range(1, grid-1):
            if pixel_count[j, i] < 10:
                continue
            val = norm_mom[j, i]
            if np.sign(val) == -major_sign and abs(val) > abs(best_val):
                best_val = val
                best_cell = (i, j)

    if best_cell is None or abs(best_val) < 0.5:
        return None, None, 0

    ci, cj = best_cell
    x0, x1 = ci * bw, min(W, (ci+1) * bw)
    y0, y1 = cj * bh, min(H, (cj+1) * bh)
    centroids = []
    for g in grays:
        region = g[y0:y1, x0:x1]
        m = region < th
        if m.sum() < 5:
            continue
        yy, xx = np.where(m)
        centroids.append((xx.mean() + x0, yy.mean() + y0))
    if centroids:
        pts = np.array(centroids)
        return float(np.median(pts[:,0])), float(np.median(pts[:,1])), best_val
    return ci * bw + bw/2.0, cj * bh + bh/2.0, best_val


def _grid_find_opposite(fr, major_sign, grid=6, th=170, grays=None):
    #角动量中找反向旋转最强的块 返回该块内暗像素的精确质心
    norm_mom, pixel_count = _grid_angular_momentum(fr, grid, th, grays=grays)
    H, W = fr[0].shape[:2]
    bh, bw = H // grid, W // grid

    best_val, best_cell = 0, None
    for j in range(1, grid-1):
        for i in range(1, grid-1):
            if pixel_count[j, i] < 10:
                continue
            val = norm_mom[j, i]
            if np.sign(val) == -major_sign and abs(val) > abs(best_val):
                best_val = val
                best_cell = (i, j)

    if best_cell is None or abs(best_val) < 0.5:
        return None, None, 0

    #暗像素质心
    ci, cj = best_cell
    x0, x1 = ci * bw, min(W, (ci+1) * bw)
    y0, y1 = cj * bh, min(H, (cj+1) * bh)
    if grays is None:
        grays = _gray_frames(fr)
    centroids = []
    for g in grays:
        region = g[y0:y1, x0:x1]
        m = region < th
        if m.sum() < 5:
            continue
        yy, xx = np.where(m)
        centroids.append((xx.mean() + x0, yy.mean() + y0))
    if centroids:
        pts = np.array(centroids)
        return float(np.median(pts[:,0])), float(np.median(pts[:,1])), best_val
    return ci * bw + bw/2.0, cj * bh + bh/2.0, best_val

def _track_bgsub(fr, delta=20, min_area=40, match_r=35, grays=None):
    #背景差分追踪
    H, W = fr[0].shape[:2]
    if grays is None:
        grays = _gray_frames(fr)
    bg = _median_bg(grays)

    # 并行处理所有帧
    n = len(grays)
    results = [None] * n
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_label_full, (bg - g) > delta, min_area): i for i, g in enumerate(grays)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = (np.zeros((0, 0), np.int32), np.empty((0, 2), np.float64), np.zeros(0, np.int32))

    fcent = []; flab = []; fsizes = []
    for lab, cents, sizes in results:
        if len(cents) == 0:
            fcent.append(np.empty((0, 2))); flab.append(None); fsizes.append(np.zeros(0))
        else:
            fcent.append(cents); flab.append(lab); fsizes.append(sizes)

    results = []
    seeds = []
    for t0 in range(min(6, len(fr))):
        if flab[t0] is None or len(fcent[t0]) == 0:
            continue
        for idx in range(len(fcent[t0])):
            sx, sy = fcent[t0][idx][0], fcent[t0][idx][1]
            if sx < 15 or sx > W - 15 or sy < 15 or sy > H - 15:
                continue
            if any(np.hypot(sx - p[0], sy - p[1]) < 25 for p in seeds):
                continue
            area0 = fsizes[t0][idx]

            cur = np.array([sx, sy])
            track = [(t0, cur.copy())]
            for t in range(t0 + 1, len(fcent)):
                P = fcent[t]
                if len(P) == 0:
                    continue
                d = np.hypot(P[:, 0] - cur[0], P[:, 1] - cur[1])
                k = int(np.argmin(d))
                if d[k] < match_r:
                    cur = P[k]; track.append((t, cur.copy()))
            if len(track) < 6:
                continue
            seeds.append((sx, sy))
            ts = np.array([p[0] for p in track], float)
            pts = np.array([p[1] for p in track], float)
            pcx, pcy = pts.mean(axis=0)
            try:
                fcx, fcy, r = _fit_circle(pts)
            except Exception:
                fcx, fcy, r = pcx, pcy, 0.0
            if 5 < r < 250:
                cx, cy = fcx, fcy
                is_circle = True
            else:
                cx, cy = pcx, pcy
                is_circle = False
            ang = np.unwrap(np.arctan2(pts[:, 1] - cy, pts[:, 0] - cx))
            da = np.diff(ang)
            total_disp = float(ang[-1] - ang[0])
            med = float(np.median(da))
            mad = float(np.median(np.abs(da - med))) + 1e-6
            good = np.abs(da - med) < 3.5 * mad
            rr = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
            rad_err = float(np.median(np.abs(rr - np.median(rr))) / (np.median(rr) + 1e-6))
            cons = float(abs(np.mean(np.sign(da))))
            conf = cons / (1.0 + 10.0 * rad_err)
            if t0 > 0 and is_circle and len(ts) >= 6:
                kA = np.polyfit(ts, ang, 1)
                a0 = float(np.polyval(kA, 0.0))
                rmed = float(np.median(rr))
                x0 = cx + rmed * np.cos(a0)
                y0 = cy + rmed * np.sin(a0)
                if 15 <= x0 <= W - 15 and 15 <= y0 <= H - 15:
                    sx, sy = x0, y0
            results.append({'cx': sx, 'cy': sy, 'omega': med, 'conf': conf,
                            'cons': cons, 'rad': rad_err, 'r': r,
                            'total_disp': total_disp,
                            'type': 'circle' if is_circle else 'noncircle',
                            'area': area0, 'compact': 1.0, 'n': len(track), 'th': 0})
    return results

def _bgsub_minority_from_tracks(tracks):
    #从bgsub轨迹中找清晰图形
    strong = [r for r in tracks
              if abs(r['total_disp']) > 4.0 and r['conf'] > 0.3 and r['area'] >= 40]
    dedup = []
    for r in sorted(strong, key=lambda r: -abs(r['total_disp']) * r['conf']):
        if not any(np.hypot(r['cx'] - d['cx'], r['cy'] - d['cy']) < 30 for d in dedup):
            dedup.append(r)
    pos = [r for r in dedup if r['total_disp'] > 0]
    neg = [r for r in dedup if r['total_disp'] < 0]
    if not dedup or len(pos) == len(neg) or min(len(pos), len(neg)) == 0:
        return None
    minority = neg if len(neg) < len(pos) else pos
    return max(minority, key=lambda r: r['conf'])

def _bgsub_find_minority(fr, grays=None):
    #背景差判定
    try:
        tracks = _track_bgsub(fr, delta=12, min_area=15, match_r=35, grays=grays)
    except Exception:
        return None
    return _bgsub_minority_from_tracks(tracks)

def driftodd_predict_v8(fr):
    _V8_DEBUG.clear()
    _V8_DEBUG['frames'] = len(fr)
    H, W = fr[0].shape[:2]

    #预计算灰度帧
    grays = _gray_frames(fr)

    #用背景差分判断
    try:
        bg_tracks = _track_bgsub(fr, delta=12, min_area=15, match_r=35, grays=grays)
    except Exception:
        bg_tracks = []
    bg_tgt = _bgsub_minority_from_tracks(bg_tracks)
    if bg_tgt is not None:
        _V8_DEBUG['selected'] = ('bgsub_minority', bg_tgt['cx'], bg_tgt['cy'],
                                 bg_tgt['total_disp'])
        return bg_tgt['cx'], bg_tgt['cy']

    #角动量fallback
    norm_mom, pixel_count = _grid_angular_momentum(fr, grid=6, th=170, grays=grays)
    if pixel_count.max() >= 10:
        major_sign = 1 if norm_mom.sum() >= 0 else -1
        gx, gy, gval = _grid_find_opposite_from_mom(
            norm_mom, pixel_count, major_sign, fr, grays, grid=6, th=170)
        if gx is not None:
            _V8_DEBUG['selected'] = ('grid_primary', gx, gy, gval)
            return gx, gy

    # 阈值追踪
    all_results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_track_at_threshold, fr, th, 10, 35, grays): th
                   for th in [100, 120, 140, 170]}
        for fut in as_completed(futures):
            try:
                all_results.extend(fut.result())
            except Exception:
                pass

    clean = [r for r in all_results if r['compact'] >= 0.15]
    large_merged = [r for r in all_results if r['compact'] < 0.15 and r['area'] > 200]

    clean.sort(key=lambda r: (r['th'], -r['conf']))
    deduped = []
    for c in clean:
        if not any(np.hypot(c['cx']-d['cx'], c['cy']-d['cy']) < 20 for d in deduped):
            deduped.append(c)
    circles = deduped
    _V8_DEBUG['results'] = [{k: r[k] for k in
        ('omega','conf','cons','cx','cy','compact','th','total_disp','area','type')} for r in circles]

    if len(circles) < 2:
        _V8_DEBUG['selected'] = None
        return None, None

    DISP_TH = 0.5
    rots = [r for r in circles if abs(r['total_disp']) > DISP_TH]
    _V8_DEBUG['n_rots'] = len(rots)

    if len(rots) == 0:
        st = [r for r in circles if r['conf'] > 0.02]
        if st:
            b = max(st, key=lambda r: r['conf'])
            _V8_DEBUG['selected'] = ('st', b['cx'], b['cy'])
            return b['cx'], b['cy']
        _V8_DEBUG['selected'] = None
        return None, None

    pos = [r for r in rots if r['total_disp'] > 0]
    neg = [r for r in rots if r['total_disp'] < 0]
    _V8_DEBUG['pos'] = len(pos)
    _V8_DEBUG['neg'] = len(neg)

    if len(pos) != len(neg) and min(len(pos), len(neg)) > 0:
        minority = neg if len(neg) < len(pos) else pos
        #过滤真实形状需有意义的旋转
        real_minority = [r for r in minority
                        if abs(r['total_disp']) > 1.5 and r['conf'] > 0.1 and
                        (r['th'] <= 140 or r['area'] >= 50)]
        if real_minority:
            tgt = max(real_minority, key=lambda r: r['conf'])
            _V8_DEBUG['selected'] = ('minority', tgt['cx'], tgt['cy'], tgt['total_disp'])
            return tgt['cx'], tgt['cy']

    vote = sum(np.sign(r['total_disp']) * r['conf'] for r in rots)
    major_sign = 1 if vote >= 0 else -1
    _V8_DEBUG['vote'] = vote

    #总位移反向者
    opposite = [r for r in circles if np.sign(r['total_disp']) == -major_sign
                and abs(r['total_disp']) > 1.0 and r['conf'] > 0.1
                and (r['th'] <= 140 or r['area'] >= 50)]
    if opposite:
        best = max(opposite, key=lambda r: abs(r['total_disp']) * r['conf'])
        _V8_DEBUG['selected'] = ('opposite_disp', best['cx'], best['cy'], best['total_disp'])
        return best['cx'], best['cy']

    #低阈值重扫
    low_results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_track_at_threshold, fr, th, 8, 35, grays): th
                   for th in [80, 90]}
        for fut in as_completed(futures):
            try:
                low_results.extend(fut.result())
            except Exception:
                pass
    low_clean = [r for r in low_results if r['compact'] >= 0.12]
    #去重
    low_new = []
    for r in low_clean:
        if not any(np.hypot(r['cx']-c['cx'], r['cy']-c['cy']) < 25 for c in circles):
            low_new.append(r)
    #找反向旋转
    low_opposite = [r for r in low_new
                   if np.sign(r['total_disp']) == -major_sign and abs(r['total_disp']) > 1.0]
    if low_opposite:
        best = max(low_opposite, key=lambda r: abs(r['total_disp']) * r['conf'])
        _V8_DEBUG['selected'] = ('low_th_opposite', best['cx'], best['cy'], best['total_disp'])
        return best['cx'], best['cy']

    #背景差分追踪
    bg_opposite = [r for r in bg_tracks
                   if np.sign(r['total_disp']) == -major_sign
                   and abs(r['total_disp']) > 3.0 and r['conf'] > 0.5
                   and r['area'] >= 40]
    #去重
    bg_dedup = []
    for r in sorted(bg_opposite, key=lambda r: -abs(r['total_disp']) * r['conf']):
        if not any(np.hypot(r['cx'] - d['cx'], r['cy'] - d['cy']) < 25 for d in bg_dedup):
            bg_dedup.append(r)
    if bg_dedup:
        best = bg_dedup[0]
        _V8_DEBUG['selected'] = ('bgsub_opposite', best['cx'], best['cy'], best['total_disp'])
        return best['cx'], best['cy']

    #角动量扫描
    gx, gy, gval = _grid_find_opposite(fr, major_sign, grays=grays)
    _V8_DEBUG['grid_angmom'] = (gx, gy, gval)
    if gx is not None:
        _V8_DEBUG['selected'] = ('grid_opposite', gx, gy, gval)
        return gx, gy

    #面积异常
    if len(rots) >= 3:
        areas = np.array([r['area'] for r in rots])
        med_area = np.median(areas)
        outliers = [r for r in rots if r['area'] > med_area * 3.0]
        if outliers:
            best = max(outliers, key=lambda r: r['area'])
            _V8_DEBUG['selected'] = ('area_outlier', best['cx'], best['cy'], best['area'])
            return best['cx'], best['cy']

    #最弱一致性
    weak = [r for r in rots if r['conf'] < 0.5 and r['area'] >= 50]
    if weak:
        best = min(weak, key=lambda r: r['conf'])
        _V8_DEBUG['selected'] = ('weak_same', best['cx'], best['cy'])
        return best['cx'], best['cy']

    tgt = max(rots, key=lambda r: r['conf'])
    _V8_DEBUG['selected'] = ('fallback_major', tgt['cx'], tgt['cy'])
    return tgt['cx'], tgt['cy']

def coherence_predict(fr):
    _V8_DEBUG.clear()
    _V8_DEBUG['frames'] = len(fr)
    H, W = fr[0].shape[:2]
    bframes = [point_centers(f, 'rgb') for f in fr]
    gw, gh = 10, 8
    bh, bw = H // gh, W // gw
    cnt = np.zeros(gh * gw, int)
    mov = np.zeros(gh * gw, int)
    for t in range(len(fr) - 1):
        P0, P1 = bframes[t], bframes[t + 1]
        if len(P0) == 0 or len(P1) == 0:
            continue
        d2, _ = cKDTree(P1).query(P0, k=1)
        moved = d2 > 3.0
        ji = (P0[:, 1] // bh).astype(int) * gw + (P0[:, 0] // bw).astype(int)
        ok = (ji >= 0) & (ji < gh * gw)
        np.add.at(cnt, ji[ok], 1)
        np.add.at(mov, ji[ok & moved], 1)
    with np.errstate(divide='ignore', invalid='ignore'):
        rate = np.where(cnt > 15, mov / np.maximum(cnt, 1), 1.0)
    rate = rate.reshape(gh, gw)
    rate[0, :] = 1.0; rate[-1, :] = 1.0
    rate[:, 0] = 1.0; rate[:, -1] = 1.0
    j, i = np.unravel_index(np.argmin(rate), rate.shape)
    j0, j1 = max(0, j - 1), min(gh, j + 2)
    i0, i1 = max(0, i - 1), min(gw, i + 2)
    fgw = 4
    fbh, fbw = bh // fgw, bw // fgw
    cnt2 = np.zeros(gh * fgw * gw * fgw, int)
    mov2 = np.zeros(gh * fgw * gw * fgw, int)
    for t in range(len(fr) - 1):
        P0, P1 = bframes[t], bframes[t + 1]
        if len(P0) == 0 or len(P1) == 0:
            continue
        d2, _ = cKDTree(P1).query(P0, k=1)
        moved = d2 > 3.0
        jj = (P0[:, 1] // fbh).astype(int)
        ii = (P0[:, 0] // fbw).astype(int)
        sel = (jj >= j0 * fgw) & (jj < j1 * fgw) & (ii >= i0 * fgw) & (ii < i1 * fgw)
        ji2 = jj[sel] * (gw * fgw) + ii[sel]
        mv2 = moved[sel]
        np.add.at(cnt2, ji2, 1)
        np.add.at(mov2, ji2[mv2], 1)
    with np.errstate(divide='ignore', invalid='ignore'):
        rate2 = np.where(cnt2 > 3, mov2 / np.maximum(cnt2, 1), 1.0)
    r2 = rate2.reshape(gh * fgw, gw * fgw)
    mg_j = max(1, int(36 // fbh))
    mg_i = max(1, int(36 // fbw))
    r2[:mg_j, :] = 1.0; r2[-mg_j:, :] = 1.0
    r2[:, :mg_i] = 1.0; r2[:, -mg_i:] = 1.0
    kk0, kk1 = max(0, j * fgw - 4), min(gh * fgw, (j + 1) * fgw + 4)
    ll0, ll1 = max(0, i * fgw - 4), min(gw * fgw, (i + 1) * fgw + 4)
    r2[:kk0, :] = 1.0; r2[kk1:, :] = 1.0
    r2[:, :ll0] = 1.0; r2[:, ll1:] = 1.0
    #用窗口内低移动率格的加权质心
    c2 = cnt2.reshape(gh * fgw, gw * fgw)
    valid = np.where(c2 >= 4, r2, np.inf)
    if np.isfinite(valid).any():
        rmin = valid.min()
        mask = (r2 <= rmin + 0.15) & (c2 >= 4)
        ys, xs = np.where(mask)
        if len(ys) > 0:
            w = c2[mask].astype(float)
            x = float(((xs * fbw + fbw / 2.0) * w).sum() / w.sum())
            y = float(((ys * fbh + fbh / 2.0) * w).sum() / w.sum())
            _V8_DEBUG['selected'] = ('coherence_centroid', x, y, float(rmin))
            return min(max(x, 25), W - 25), min(max(y, 25), H - 25)
    # 回退coarse格中心
    x = min(max(i * bw + bw / 2.0, 25), W - 25)
    y = min(max(j * bh + bh / 2.0, 25), H - 25)
    _V8_DEBUG['selected'] = ('coherence_coarse', x, y, float(rate[j, i]))
    return x, y

def solve(img_bytes, typ):
    fr = load_gif(img_bytes)
    if typ == 'driftodd':
        return driftodd_predict_v8(fr)
    elif typ == 'coherence':
        return coherence_predict(fr)
    return None, None

    main()
