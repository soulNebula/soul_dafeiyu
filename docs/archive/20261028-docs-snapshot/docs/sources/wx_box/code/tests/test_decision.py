# -*- coding: utf-8 -*-
"""12/13 决策与弱验证测试。"""
from agi.selflearn import decision, evaluation


def test_decision_lcb_quadrants():
    low = decision.decide(1, 2, score=0.8)
    assert low["action"] == "explore"
    high = decision.decide(50, 50, score=0.8)
    assert high["action"] == "do"


def test_evaluation_paired_and_calibration():
    r = evaluation.paired_win_rate([True, True, False, True])
    assert r["win_rate"] == 0.75
    c = evaluation.calibrate([0.1, 0.9], [False, True])
    assert c["ok"] is True and len(c["bins"]) == 5
