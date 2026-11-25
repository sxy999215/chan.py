from decimal import Decimal
from typing import List, Optional, Union, overload

from Common.CEnum import FX_CHECK_METHOD, FX_TYPE, KLINE_DIR
from KLine.KLine import CKLine

from .Bi import CBi
from .BiConfig import CBiConfig


def _seg_visible_low(bottom: CKLine, klc: CKLine) -> float:
    # 店主规则（2026-11-25）：60% 规则的分母底，取 [bottom, klc] 段内原始 K 线可见最低价，
    # 而不是底分型的合并低——合并会吃掉段内更低的原始极值（如 3607.52 被并入 2233 号合并 K 线）。
    low = bottom.get_klu_min_low()
    cur = bottom.next
    steps = 0
    while cur is not None and cur.idx <= klc.idx and steps < 100000:
        low = min(low, cur.get_klu_min_low())
        if cur.idx == klc.idx:
            return low
        cur = cur.next
        steps += 1
    # 链路走不到 klc 时退回底分型合并低（旧口径），不让函数失效。
    return bottom.low


def is_invalid_top(klc: CKLine, bottom: CKLine, right_low: Optional[float] = None) -> bool:
    if klc.fx != FX_TYPE.TOP or klc.next is None or bottom.fx != FX_TYPE.BOTTOM:
        return False
    # 十进制价格比较，恰好 60% 也排除。
    top = Decimal(str(klc.high))
    span = top - Decimal(str(_seg_visible_low(bottom, klc)))
    next_low = Decimal(str(klc.next.low if right_low is None else right_low))
    return span > 0 and 5 * (top - next_low) >= 3 * span


class CBiList:
    def __init__(self, bi_conf=CBiConfig()):
        self.bi_list: List[CBi] = []
        self.last_end = None  # 最后一笔的尾部
        self._end_anchor = None  # 店主规则（2026-11-25）：下笔端点锚点，正常成笔/延伸确立时的底分型；规则移动端点时不更新
        self.config = bi_conf
        self.latest_top_bottom: Optional[CKLine] = None

        self.free_klc_lst = []  # 仅仅用作第一笔未画出来之前的缓存，为了获得更精准的结果而已，不加这块逻辑其实对后续计算没太大影响

    def __str__(self):
        return "\n".join([str(bi) for bi in self.bi_list])

    def __iter__(self):
        yield from self.bi_list

    @overload
    def __getitem__(self, index: int) -> CBi: ...

    @overload
    def __getitem__(self, index: slice) -> List[CBi]: ...

    def __getitem__(self, index: Union[slice, int]) -> Union[List[CBi], CBi]:
        return self.bi_list[index]

    def __len__(self):
        return len(self.bi_list)

    def adjust_endpoint_prices(self, klc_list: List[CKLine]):
        # 端点取笔内原始极值，共用端点同步价格和画线时间。
        for bi in self:
            bi.set_endpoint_prices()
            bi.set_endpoint_times()

        begin_price = None
        begin_time = None
        for idx, bi in enumerate(self):
            next_bi = self[idx + 1] if idx + 1 < len(self) else None
            stop_idx = next_bi.end_klc.idx if next_bi else len(klc_list) - 1
            end_time = bi.get_end_klu().time.ts
            next_time = next_bi.get_end_klu().time.ts if next_bi else float("inf")
            is_down = bi.is_down()
            opposite_fx = FX_TYPE.TOP if is_down else FX_TYPE.BOTTOM
            reference = begin_price if begin_price is not None else bi.raw_begin_val()
            original = extreme = bi.raw_end_val()
            extreme_time = None

            for klc in klc_list[bi.end_klc.idx:stop_idx + 1]:
                peak_time = None
                if klc.fx == opposite_fx:
                    peak_time = klc.get_peak_klu(is_high=is_down).time.ts
                for klu in klc.lst:
                    time = klu.time.ts
                    if end_time < time < next_time:
                        if is_down and klu.close <= reference and klu.low < extreme:
                            extreme = klu.low
                            extreme_time = klu.time
                        elif not is_down and klu.close >= reference and klu.high > extreme:
                            # 防止被当前上笔拒绝的顶通过原始极值校正成为显示端点。
                            if klu.high != klc.high or not is_invalid_top(klc, bi.begin_klc):
                                extreme = klu.high
                                extreme_time = klu.time
                    # 同一根原始 K 线上的分型只影响后续 K 线。
                    if peak_time is not None and time == peak_time and end_time <= time:
                        reference = klc.high if is_down else klc.low

            end_price = extreme if extreme != original else None
            bi.set_endpoint_prices(begin=begin_price, end=end_price)
            bi.set_endpoint_times(begin=begin_time, end=extreme_time)
            begin_price = end_price
            begin_time = extreme_time

    def try_create_first_bi(self, klc: CKLine) -> bool:
        for exist_free_klc in self.free_klc_lst:
            if exist_free_klc.fx == klc.fx:
                continue
            if self.can_make_bi(klc, exist_free_klc):
                self.add_new_bi(exist_free_klc, klc)
                self.last_end = klc
                return True
        self.free_klc_lst.append(klc)
        self.last_end = klc
        return False

    def update_bi(self, klc: CKLine, last_klc: CKLine, cal_virtual: bool) -> bool:
        # klc: 倒数第二根klc
        # last_klc: 倒数第1根klc
        flag1 = self.update_bi_sure(klc)
        self.latest_top_bottom = (
            self[-1].begin_klc
            if self.bi_list and self[-1].is_up() and self[-1].end_klc is klc
            else None
        )
        if cal_virtual:
            flag2 = self.try_add_virtual_bi(last_klc)
            return flag1 or flag2
        else:
            return flag1

    def can_update_peak(self, klc: CKLine):
        if self.config.bi_allow_sub_peak or len(self.bi_list) < 2:
            return False
        if self.bi_list[-1].is_down() and klc.high < self.bi_list[-1].raw_begin_val():
            return False
        if self.bi_list[-1].is_up() and klc.low > self.bi_list[-1].raw_begin_val():
            return False
        if not end_is_peak(self.bi_list[-2].begin_klc, klc):
            return False
        if self[-1].is_down() and self[-1].raw_end_val() < self[-2].raw_begin_val():
            return False
        if self[-1].is_up() and self[-1].raw_end_val() > self[-2].raw_begin_val():
            return False
        return True

    def update_peak(self, klc: CKLine, for_virtual=False):
        if not self.can_update_peak(klc):
            return False
        _tmp_last_bi = self.bi_list[-1]
        self.bi_list.pop()
        # 店主规则（2026-11-25）：弹笔后锚点与新的最后一笔同步，否则锚点指向已被 pop 的笔的端点。
        self._end_anchor = self.bi_list[-1].end_klc if (self.bi_list and self.bi_list[-1].is_down()) else None
        if not self.try_update_end(klc, for_virtual=for_virtual):
            self.bi_list.append(_tmp_last_bi)
            self._end_anchor = _tmp_last_bi.end_klc if _tmp_last_bi.is_down() else None
            return False
        else:
            if for_virtual:
                self.bi_list[-1].append_sure_end(_tmp_last_bi.end_klc)
            return True

    def update_bi_sure(self, klc: CKLine) -> bool:
        # klc: 倒数第二根klc
        _tmp_end = self.get_last_klu_of_last_bi()
        self.delete_virtual_bi()
        # 返回值：是否出现新笔
        if klc.fx == FX_TYPE.UNKNOWN:
            return _tmp_end != self.get_last_klu_of_last_bi()  # 虚笔是否有变
        if self.last_end is None or len(self.bi_list) == 0:
            return self.try_create_first_bi(klc)
        if klc.fx == self.last_end.fx:
            old_end = self.last_end
            if not self.try_update_end(klc):
                # 店主规则（2026-11-25）：下笔端点移动——旧低后出现新低且之间 ≥20 根合并 K 线（idx 差 ≥21）时，端点移到新低。
                if not self._try_move_end_to_new_low(klc):
                    return False
                # 规则移端后重测拆笔不得动锚点：锚点须留在移端前的旧低，否则重测经 add_new_bi 会把锚点推到规则移端点上。
                saved_anchor = self._end_anchor
                if klc.idx > old_end.idx:
                    self.retest_extended_bi(old_end, klc)
                self._end_anchor = saved_anchor
                return True
            if klc.idx > old_end.idx:
                self.retest_extended_bi(old_end, klc)
            return True
        elif self.can_make_bi(klc, self.last_end):
            self.add_new_bi(self.last_end, klc)
            self.last_end = klc
            return True
        elif self.update_peak(klc):
            return True
        return _tmp_end != self.get_last_klu_of_last_bi()

    def _try_move_end_to_new_low(self, klc: CKLine) -> bool:
        # 店主规则（2026-11-25）：下笔端点在旧低（锚点），后来出现新低，
        # 锚点与新低之间合并 K 线 ≥20 根，且新低比之间所有底分型都低时，下笔端点移到新低。
        # 只做下笔，上笔不对称处理（店主未定）。规则移动端点时锚点不更新。
        if len(self.bi_list) == 0 or not self[-1].is_down():
            return False
        if klc.fx != FX_TYPE.BOTTOM:
            return False
        anchor = self._end_anchor
        if anchor is None or klc.idx - anchor.idx < 21:
            return False
        cur = anchor.next
        steps = 0
        while cur is not None and cur.idx < klc.idx and steps < 100000:
            if cur.fx == FX_TYPE.BOTTOM and cur.low <= klc.low:
                return False
            cur = cur.next
            steps += 1
        # fail-closed：只有真正走到 klc 才算校验完成，链断裂、步数耗尽、idx 跳过时一律不移动。
        if cur is None or cur.idx != klc.idx:
            return False
        self[-1].update_new_end(klc)
        self.last_end = klc
        return True

    def retest_extended_bi(self, old_end: CKLine, new_end: CKLine):
        skipped = old_end.next
        while skipped.idx < new_end.idx and skipped.fx == FX_TYPE.UNKNOWN:
            skipped = skipped.next
        if skipped.idx == new_end.idx:
            return

        begin = self[-1].begin_klc
        fractals = []
        klc = begin
        while klc.idx < new_end.idx:
            if klc.fx != FX_TYPE.UNKNOWN:
                fractals.append(klc)
            klc = klc.next

        def extremity(klc: CKLine):
            return klc.high if klc.fx == FX_TYPE.TOP else -klc.low

        def can_link(start: CKLine, end: CKLine):
            # strict 豁免时也要满足 CBi 的严格价格方向。
            top, bottom = (start, end) if start.fx == FX_TYPE.TOP else (end, start)
            return top.high > bottom.low and self.can_make_bi(end, start)

        # fractals 按 idx 升序排列，max 在极值相同的情况下取最早的 idx。
        anchor = max(
            (klc for klc in fractals if klc.fx == begin.fx
             and (len(self) == 1 or not is_invalid_top(klc, self[-2].begin_klc))),
            key=extremity,
        )
        anchor_unchanged = anchor.idx == begin.idx
        endpoints = [anchor]
        while True:
            start = endpoints[-1]
            candidate = max(
                (klc for klc in fractals if klc.idx > start.idx and klc.fx != start.fx
                 and can_link(start, klc)),
                key=extremity, default=None,
            )
            if candidate is None:
                break
            endpoints.append(candidate)

        # 新旧端点同型。若末个候选也同型，由新端点接替它。
        if endpoints[-1].fx == new_end.fx:
            endpoints.pop()
        if anchor_unchanged and len(endpoints) == 1:
            return

        # 提交前检查收尾和起点移动影响的前一笔。失败时保留原延长结果。
        if not can_link(endpoints[-1], new_end):
            return
        if len(self) > 1 and not anchor_unchanged:
            if not can_link(self[-2].begin_klc, anchor):
                return
            self[-2].update_new_end(anchor)
        endpoints.append(new_end)
        self[-1].set(anchor, endpoints[1])
        for start, end in zip(endpoints[1:], endpoints[2:]):
            self.add_new_bi(start, end)

    def delete_virtual_bi(self):
        if len(self) > 0 and not self.bi_list[-1].is_sure:
            sure_end_list = [klc for klc in self.bi_list[-1].sure_end]
            if len(sure_end_list):
                self.bi_list[-1].restore_from_virtual_end(sure_end_list[0])
                self.last_end = self[-1].end_klc
                for sure_end in sure_end_list[1:]:
                    self.add_new_bi(self.last_end, sure_end, is_sure=True)
                    self.last_end = self[-1].end_klc
            else:
                del self.bi_list[-1]
        self.last_end = self[-1].end_klc if len(self) > 0 else None
        if len(self) > 0:
            self[-1].next = None

    def try_add_virtual_bi(self, klc: CKLine, need_del_end=False):
        if need_del_end:
            self.delete_virtual_bi()
        if len(self) == 0:
            return False
        if klc.idx == self[-1].end_klc.idx:
            return False
        if (
            (self[-1].is_up() and klc.high >= self[-1].end_klc.high
             and not is_invalid_top(klc, self[-1].begin_klc))
            or (self[-1].is_down() and klc.low <= self[-1].end_klc.low)
        ):
            # 更新最后一笔
            self.bi_list[-1].update_virtual_end(klc)
            return True
        _tmp_klc = klc
        while _tmp_klc and _tmp_klc.idx > self[-1].end_klc.idx:
            assert _tmp_klc is not None
            if self.can_make_bi(_tmp_klc, self[-1].end_klc, for_virtual=True):
                # 新增一笔
                self.add_new_bi(self.last_end, _tmp_klc, is_sure=False)
                return True
            elif self.update_peak(_tmp_klc, for_virtual=True):
                return True
            _tmp_klc = _tmp_klc.pre
        return False

    def add_new_bi(self, pre_klc, cur_klc, is_sure=True):
        self.bi_list.append(CBi(pre_klc, cur_klc, idx=len(self.bi_list), is_sure=is_sure))
        if len(self.bi_list) >= 2:
            self.bi_list[-2].next = self.bi_list[-1]
            self.bi_list[-1].pre = self.bi_list[-2]
        # 店主规则（2026-11-25）：新笔确立时按方向设端点锚点（下笔＝cur_klc，上笔＝None）。
        # 只对确定笔设锚点，虚笔不碰锚点（虚笔端点不是确定端点，删虚笔后锚点会悬空）。
        if is_sure:
            self._end_anchor = cur_klc if self.bi_list[-1].is_down() else None

    def satisfy_bi_span(self, klc: CKLine, last_end: CKLine):
        bi_span = self.get_klc_span(klc, last_end)
        if self.config.is_strict:
            return bi_span >= 4
        uint_kl_cnt = 0
        tmp_klc = last_end.next
        while tmp_klc:
            uint_kl_cnt += len(tmp_klc.lst)
            if not tmp_klc.next:  # 最后尾部虚笔的时候，可能klc.idx == last_end.idx+1
                return False
            if tmp_klc.next.idx < klc.idx:
                tmp_klc = tmp_klc.next
            else:
                break
        return bi_span >= 3 and uint_kl_cnt >= 3

    def get_klc_span(self, klc: CKLine, last_end: CKLine) -> int:
        span = klc.idx - last_end.idx
        if not self.config.gap_as_kl:
            return span
        if span >= 4:  # 加速运算，如果span需要真正精确的值，需要去掉这一行
            return span
        tmp_klc = last_end
        while tmp_klc and tmp_klc.idx < klc.idx:
            if tmp_klc.has_gap_with_next():
                span += 1
            tmp_klc = tmp_klc.next
        return span

    def can_make_bi(self, klc: CKLine, last_end: CKLine, for_virtual: bool = False):
        satisify_span = True if self.config.bi_algo == 'fx' else self.satisfy_bi_span(klc, last_end)
        if not satisify_span:
            return False
        # 店主规则（2026-11-24）：下笔中间至少 7 根合并 K 线时跳过 strict 分型校验。
        # 店主规则（2026-11-24 针排除）：底分型合并 K 线的收盘价取末根单位 K 线的 close，高于顶分型最高价时仍做 strict 校验。
        skip_fx_check = (
            self.config.bi_fx_check == FX_CHECK_METHOD.STRICT
            and last_end.fx == FX_TYPE.TOP
            and (klc.dir == KLINE_DIR.DOWN if for_virtual else klc.fx == FX_TYPE.BOTTOM)
            and self.get_klc_span(klc, last_end) - 1 >= 7
            and klc[-1].close <= last_end.high
        )
        if not skip_fx_check and not last_end.check_fx_valid(klc, self.config.bi_fx_check, for_virtual):
            return False
        if self.config.bi_end_is_peak and not end_is_peak(last_end, klc):
            return False
        return not is_invalid_top(klc, last_end)

    def try_update_end(self, klc: CKLine, for_virtual=False) -> bool:
        def check_top(klc: CKLine, for_virtual):
            if for_virtual:
                return klc.dir == KLINE_DIR.UP
            else:
                return klc.fx == FX_TYPE.TOP

        def check_bottom(klc: CKLine, for_virtual):
            if for_virtual:
                return klc.dir == KLINE_DIR.DOWN
            else:
                return klc.fx == FX_TYPE.BOTTOM

        if len(self.bi_list) == 0:
            return False
        last_bi = self.bi_list[-1]
        if (last_bi.is_up() and check_top(klc, for_virtual) and klc.high >= last_bi.raw_end_val()
            and not is_invalid_top(klc, last_bi.begin_klc)) or \
           (last_bi.is_down() and check_bottom(klc, for_virtual) and klc.low <= last_bi.raw_end_val()):
            last_bi.update_virtual_end(klc) if for_virtual else last_bi.update_new_end(klc)
            self.last_end = klc
            # 店主规则（2026-11-25）：下笔延伸成功（非 virtual）时更新端点锚点。
            if not for_virtual and last_bi.is_down():
                self._end_anchor = klc
            return True
        else:
            return False

    def get_last_klu_of_last_bi(self) -> Optional[int]:
        return self.bi_list[-1].get_end_klu().idx if len(self) > 0 else None


def end_is_peak(last_end: CKLine, cur_end: CKLine) -> bool:
    if last_end.fx == FX_TYPE.BOTTOM:
        cmp_thred = cur_end.high  # 或者严格点选择get_klu_max_high()
        klc = last_end.get_next()
        while True:
            if klc.idx >= cur_end.idx:
                return True
            if klc.high > cmp_thred:
                return False
            klc = klc.get_next()
    elif last_end.fx == FX_TYPE.TOP:
        cmp_thred = cur_end.low  # 或者严格点选择get_klu_min_low()
        klc = last_end.get_next()
        while True:
            if klc.idx >= cur_end.idx:
                return True
            if klc.low < cmp_thred:
                return False
            klc = klc.get_next()
    return True
