#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# 用法: python main.py "<auth_url_or_ticket>" | ticket.txt | --generate N
import sys
import os
import time
import json
import argparse
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import captcha_solver as CS
import auth_client as AUTH
import link_generator as LG

CAPTCHA_MAX_RETRIES = 1
FALLBACK_SERVICES = [3]
MAX_ROUNDS = 3


# 计时器
class Timer:
    # 累计各阶段耗时
    def __init__(self):
        self.phases = {}  # 阶段名 -> 总秒数
        self._t0 = None
        self._current_phase = None

    def start(self, phase):
        # 开始计时一个阶段
        if self._current_phase is not None:
            self.stop()
        self._current_phase = phase
        self._t0 = time.time()

    def stop(self):
        # 停止当前阶段计时
        if self._current_phase is not None and self._t0 is not None:
            dt = time.time() - self._t0
            self.phases[self._current_phase] = self.phases.get(self._current_phase, 0.0) + dt
            self._current_phase = None
            self._t0 = None

    def add(self, name, seconds):
        # 直接添加耗时
        self.phases[name] = self.phases.get(name, 0.0) + seconds

    def total(self):
        return sum(self.phases.values())

    def summary(self):
        parts = []
        for name, secs in sorted(self.phases.items(), key=lambda x: -x[1]):
            pct = secs / self.total() * 100 if self.total() > 0 else 0
            parts.append(f"    {name}: {secs:.1f}s ({pct:.0f}%)")
        return '\n'.join(parts)

    def __repr__(self):
        return f"Timer({self.total():.1f}s total, {len(self.phases)} phases)"


#验证码
def get_captcha_token(session=None, verbose=True, timer=None):
    #获取验证码token
    if session is None:
        session = CS.session()

    if timer:
        timer.start('captcha')

    for attempt in range(CAPTCHA_MAX_RETRIES):
        try:
            ch = session.get(CS.API + '/challenge', timeout=10).json()
            img_url = 'https://captcha.platorelay.com' + ch['image']
            img = session.get(
                img_url,
                headers={'Referer': 'https://captcha.platorelay.com/'},
                timeout=10
            ).content

            t0 = time.time()
            x, y = CS.solve(img, ch['type'])
            dt = time.time() - t0

            if x is None:
                if verbose:
                    print(f'  [captcha] 尝试 {attempt + 1}: 无解 ({(dt * 1000):.0f}ms), 重试...', flush=True)
                continue

            r = session.post(CS.API + '/answer', json={
                'challenge_id': ch['challenge_id'],
                'x': x,
                'y': y
            }, timeout=10).json()

            if r.get('success'):
                token = r['token']
                sel = CS._V8_DEBUG.get('selected', '')
                strat = sel[0] if isinstance(sel, tuple) and sel else ''
                if timer:
                    timer.stop()
                if verbose:
                    print(f'  [captcha] {ch["type"]} @({x:.0f},{y:.0f})'
                          f' {"HIT"} [{strat}]'
                          f' {(dt * 1000):.0f}ms', flush=True)
                return token
            else:
                if verbose:
                    print(f'  [captcha] 尝试 {attempt + 1}: 未命中 ({(dt * 1000):.0f}ms), 重试...', flush=True)
        except Exception as e:
            if verbose:
                print(f'  [captcha] 错误: {e}', flush=True)

    if timer:
        timer.stop()
    raise RuntimeError(f'captcha 失败(已重试 {CAPTCHA_MAX_RETRIES} 次)')


#Metadata->Service解析
def resolve_service(ticket, session=None, verbose=True):
    #从metadata获取当前ticket使用的service值
    try:
        meta = AUTH.get_session_metadata(ticket, session=session)
        if isinstance(meta, dict):
            data = meta.get('data', meta)
            if isinstance(data, dict):
                profile = data.get('activeRevenueProfile', {})
                if isinstance(profile, dict) and 'service' in profile:
                    svc = int(profile['service'])
                    if verbose:
                        cp = profile.get('checkpointCount', '?')
                        dur = profile.get('duration', '?')
                        print(f'  [meta] service={svc} checkpointCount={cp} duration={dur}h', flush=True)
                    return svc
    except Exception as e:
        if verbose:
            print(f'  [meta] 获取失败: {e}', flush=True)
    return None


#Step推进
def do_step_with_retry(ticket, token, service=None, session=None, verbose=True, timer=None):
    #执行step失败时尝试回退
    services_to_try = []
    if service is not None:
        services_to_try.append(service)
    for svc in FALLBACK_SERVICES:
        if svc not in services_to_try:
            services_to_try.append(svc)

    if timer:
        timer.start('step')

    for svc in services_to_try:
        try:
            t0 = time.time()
            r = AUTH.do_step(ticket, token, service=svc, session=session)
            dt = time.time() - t0
            if isinstance(r, dict) and r.get('success'):
                if timer:
                    timer.stop()
                if verbose:
                    print(f'  [step] service={svc} -> 成功 ({(dt * 1000):.0f}ms)', flush=True)
                return svc, r
            if verbose:
                err = json.dumps(r)[:200] if isinstance(r, dict) else str(r)[:200]
                print(f'  [step] service={svc}: {err} ({(dt * 1000):.0f}ms)', flush=True)
        except Exception as e:
            if verbose:
                print(f'  [step] service={svc} 异常: {e}', flush=True)

    if timer:
        timer.stop()
    return None, {'success': False, 'error': 'all services failed'}


#Key提取
def check_key_in_response(ticket, session=None, verbose=True, timer=None):
    #检查会话中是否已有key
    try:
        if timer:
            timer.start('poll')
        st = AUTH.get_session_status(ticket, session=session)
        if timer:
            timer.stop()
        st_data = st.get('data', st) if isinstance(st, dict) else {}
        key = st_data.get('key', '')
        if key and key != 'KEY_NOT_FOUND':
            if verbose:
                print(f'  [key] 发现 KEY: {key}', flush=True)
            return key
        if verbose and key:
            print(f'  [key] 尚未就绪: {key}', flush=True)
    except Exception as e:
        if verbose:
            print(f'  [key] 检查异常: {e}', flush=True)
    return None


def poll_for_key(ticket, session=None, max_attempts=3, interval=0, verbose=True, timer=None):
    #轮询等待key
    for i in range(max_attempts):
        key = check_key_in_response(ticket, session=session, verbose=verbose, timer=timer)
        if key:
            return key
    return None


#主循环
def solve_chain(ticket, verbose=True, max_rounds=MAX_ROUNDS, session=None):
    #完整链路ticket->captcha->step->decode->repeat->key
    if session is None:
        session = AUTH.create_session()
    current_ticket = ticket
    current_service = None
    timer = Timer()

    for round_idx in range(max_rounds):
        if verbose:
            print(f'  [{round_idx + 1}/{max_rounds}]', flush=True)

        # 避免触发服务器限速
        if round_idx > 0:
            time.sleep(2.0)

        #并行获取meta和captcha
        if timer:
            timer.start('meta')
        meta_session = AUTH.create_session()
        meta_future = None
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                meta_future = pool.submit(resolve_service, current_ticket, meta_session, verbose)

                #同步captcha
                try:
                    token = get_captcha_token(session=session, verbose=verbose, timer=timer)
                except RuntimeError as e:
                    if verbose:
                        print(f'  [error] {e}', flush=True)
                        print(f'  [retry] 新建 session 重试 captcha...', flush=True)
                    session = AUTH.create_session()
                    try:
                        token = get_captcha_token(session=session, verbose=verbose, timer=timer)
                    except RuntimeError as e2:
                        if verbose:
                            print(f'  [error] {e2}', flush=True)
                        print(f'  [-] 第 {round_idx + 1} round captcha 失败, 跳过', flush=True)
                        continue
                #收集meta结果
                try:
                    current_service = meta_future.result(timeout=5)
                except Exception:
                    current_service = None
        finally:
            meta_session.close()
        if timer:
            timer.stop()
        if current_service is not None and verbose:
            print(f'  [service] metadata: {current_service}', flush=True)
        service, resp = do_step_with_retry(
            current_ticket, token,
            service=current_service,
            session=session,
            verbose=verbose,
            timer=timer
        )
        if service is None:
            if verbose:
                print(f'  [-] 第 {round_idx + 1} round step 全部失败, 跳过', flush=True)
            continue

        current_service = service
        #提取URL
        url = (resp.get('data') or {}).get('url', '')
        if not url:
            if verbose:
                print(f'  [-] 响应中没有 URL', flush=True)
            continue

        if verbose:
            url_short = url[:80] + '...' if len(url) > 80 else url
            print(f'  [url] {url_short}', flush=True)

        #轮询一次
        if url == 'about:blank':
            if verbose:
                print(f'  [poll]', flush=True)
            key = poll_for_key(current_ticket, session=session, verbose=verbose, timer=timer, max_attempts=1)
            if key:
                return key, timer
            if verbose:
                print(f'  [-] 轮询超时, 继续下一 round', flush=True)
            continue

        #解码r=参数->下一张ticket
        callback = AUTH.decode_callback_url(url)
        if callback:
            next_ticket = AUTH.extract_ticket_from_callback(callback)
            if next_ticket and len(next_ticket) > 50:
                if verbose:
                    print(f'  [next] 新 ticket: {next_ticket[:24]}... ({len(next_ticket)} chars)', flush=True)
                current_ticket = next_ticket
                continue

        #无r=回调->lootlabs 链接轮询 key
        if verbose:
            print(f'  [info] 无 r= 回调, 尝试轮询 key...', flush=True)
        key = check_key_in_response(current_ticket, session=session, verbose=verbose, timer=timer)
        if key:
            return key, timer
        break

    #最后检查
    key = check_key_in_response(current_ticket, session=session, verbose=verbose, timer=timer)
    if key:
        return key, timer

    if verbose:
        print(f'\n[-] 达到最大 round ({max_rounds}) 或链路中断, 未获取到 key', flush=True)
    return None, timer


# CLI
def main():
    ap = argparse.ArgumentParser(
        description='Delta自动求解器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例:
  %(prog)s "https://auth.platorelay.com/a?d=<ticket>"
  %(prog)s "<raw_ticket>"
  %(prog)s ticket.txt
  %(prog)s --generate 3
        '''
    )
    ap.add_argument('target', nargs='?', help='auth URL / ticket / 文件路径')
    ap.add_argument('--generate', '-g', type=int, default=0,
                    help='通过 Platoboost API 生成 N 条测试链接')
    ap.add_argument('--quiet', '-q', action='store_true',
                    help='静默模式 (只输出结果)')
    ap.add_argument('--max-rounds', type=int, default=MAX_ROUNDS,
                    help=f'最大 round 数 (默认 {MAX_ROUNDS})')
    ap.add_argument('--no-auto', action='store_true',
                    help='只生成链接, 不求解')
    args = ap.parse_args()

    verbose = not args.quiet

    tickets = []
    gen_start = time.time()

    if args.generate > 0:
        if verbose:
            print(f'[*] 生成 {args.generate} 条链接...', flush=True)
        try:
            urls = LG.batch_links(args.generate)
            tickets = [AUTH.extract_ticket(u) for u in urls]
            if verbose:
                print(f'[*] 成功获取 {len(tickets)} 条 ticket', flush=True)
        except Exception as e:
            print(f'[-] 生成链接失败: {e}', file=sys.stderr, flush=True)
            sys.exit(1)
    elif args.target:
        tickets.append(AUTH.extract_ticket(args.target))
    else:
        ap.print_help()
        sys.exit(1)

    if args.no_auto:
        for t in tickets:
            print(f'https://auth.platorelay.com/a?d={t}')
        return

    #逐条求解
    results = []
    for i, ticket in enumerate(tickets):
        t0 = time.time()
        key, timer = solve_chain(ticket, verbose=verbose, max_rounds=args.max_rounds,
                                 session=None)
        dt = time.time() - t0
        results.append((i, key, timer, dt))

        if key:
            print(f'\n{"=" * 60}', flush=True)
            print(f'[+] DELTA KEY #{i + 1}: {key}', flush=True)
            print(f'[+] 耗时: {dt:.1f}s', flush=True)
            print(f'[+] 阶段明细:')
            print(timer.summary())
            print(f'{"=" * 60}', flush=True)
        elif verbose:
            print(f'\n[-] 链接 {i + 1}: 未获取到 key', flush=True)
            if timer.total() > 0:
                print(f'[-] 耗时: {dt:.1f}s')
                print(f'[-] 阶段明细:')
                print(timer.summary())

    #汇总
    total_elapsed = time.time() - gen_start
    success_count = sum(1 for _, key, _, _ in results if key)
    total_timer = Timer()
    for _, _, timer, _ in results:
        for name, secs in timer.phases.items():
            total_timer.add(name, secs)

    print(f'\n{"=" * 60}', flush=True)
    print(f'[+] 汇总: {success_count}/{len(tickets)} 成功', flush=True)
    print(f'[+] 总耗时: {total_elapsed:.1f}s', flush=True)
    if len(tickets) > 0:
        print(f'[+] 平均每链接: {total_elapsed / len(tickets):.1f}s', flush=True)
    if total_timer.total() > 0:
        print(f'[+] 总阶段明细:')
        print(total_timer.summary())
    print(f'{"=" * 60}', flush=True)


if __name__ == '__main__':
    main()
