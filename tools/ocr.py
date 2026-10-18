#!/usr/bin/env python3
"""Vision OCR helpers. The only piece of the sniping toolchain this farm needs.

⭐ Extracted deliberately. The farm previously imported `step.py` - 1,500 lines
of auction-house sniping - for two functions. That dragged unrelated code, and
an unrelated audit surface, into a farm that has no business buying anything.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Quartz  # noqa: E402
import Vision  # noqa: E402
from Foundation import NSURL  # noqa: E402

import pad  # noqa: E402

OCR_MIN_H = 120       # below this height a crop counts as "short"
# ⭐ 2.5x, MEASURED - not chosen for roundness. Scored against every labelled
# sample: 1x 2/4 · 1.5x 3/4 · 2x 3/4 · 2.5x 4/4 · 3x 3/4 · 4x 3/4.
# 2x was the first guess and is wrong on a quarter of them.
OCR_SCALE = 2.5       # short crops
OCR_SCALE_TALL = 1.5  # tall crops: 2x regresses on larger text


def upscale(img, factor=OCR_SCALE):
    w = int(Quartz.CGImageGetWidth(img) * factor)
    h = int(Quartz.CGImageGetHeight(img) * factor)
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, w, h, 8, 0, cs,
                                       Quartz.kCGImageAlphaPremultipliedLast)
    if ctx is None:
        return img
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, w, h), img)
    return Quartz.CGBitmapContextCreateImage(ctx) or img


def load_image(path):
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return None
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def recognize(img):
    """CGImage -> list of VNRecognizedTextObservation."""
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        img, None)
    handler.performRequests_error_([req], None)
    return req.results() or []


def ocr(region, path="/tmp/mut-event/_o.png"):
    """Capture `region` and return its text, pipe-joined.

    `region` is (x, y, w, h) in display points, or None for the whole screen.
    """
    pad.shot(region, path)
    img = load_image(path)
    if img is None:
        return ""
    h = Quartz.CGImageGetHeight(img)
    img = upscale(img, OCR_SCALE if h < OCR_MIN_H else OCR_SCALE_TALL)
    return " | ".join(r.topCandidates_(1)[0].string() for r in recognize(img))
