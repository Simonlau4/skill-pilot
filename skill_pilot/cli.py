import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from .scan import scan
from .store import apply, backups, preview, restore, target_home
from .environment import tool_environment
from . import operations


def main():
    parser = argparse.ArgumentParser(description="Skill Pilot：本机技能管理")
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("scan", help="盘点本机 agent 和技能")
    inventory.add_argument("--json", action="store_true", help="输出完整机器可读结果")
    commands.add_parser("ui", help="打开中文看板").add_argument('--json', action='store_true')
    hub = commands.add_parser('hub', help='运行团队分享站或管理成员')
    hub_commands = hub.add_subparsers(dest='hub_command', required=True)
    serving = hub_commands.add_parser('serve', help='启动分享站')
    serving.add_argument('--host', default='0.0.0.0', help='监听地址，默认局域网所有地址')
    serving.add_argument('--port', type=int, default=8765, help='监听端口，默认 8765')
    member = hub_commands.add_parser('add-member', help='添加成员并显示一次口令')
    member.add_argument('name', help='成员显示名')
    members = hub_commands.add_parser('members', help='查看成员名单，不显示口令')
    removing = hub_commands.add_parser('remove-member', help='删除成员和他的分享')
    removing.add_argument('member_id', help='成员编号')
    for command in (serving, member, members, removing):
        command.add_argument('--data', required=True, help='分享站数据文件夹（必须指定）')
    migrate = commands.add_parser("migrate", help="预览整理或在模拟目录确认执行")
    mode = migrate.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true", help="只读生成整理预览")
    mode.add_argument("--apply", action="store_true", help="确认执行已核对的预览")
    migrate.add_argument("--plan-id")
    migrate.add_argument("--agents", nargs="+", help="要接入的 agent，默认内置档案中的 agent")
    migrate.add_argument("--choices", help="核对结果文件：choices 与 sources 两个对象")
    migrate.add_argument("--json", action="store_true")
    saved = commands.add_parser("backups", help="查看整理前备份")
    saved.add_argument("--json", action="store_true")
    cleanup_command = commands.add_parser('cleanup', help='手动挑选技能撤下，或查看已撤下的技能')
    cleanup_command.add_argument('skills', nargs='*', metavar='SKILL')
    cleanup_command.add_argument('--list', action='store_true')
    cleanup_command.add_argument('--yes', action='store_true')
    cleanup_command.add_argument('--json', action='store_true')
    withdrawn_command = commands.add_parser('restore-withdrawn', help='恢复已撤下技能的原分配')
    withdrawn_command.add_argument('skills', nargs='+')
    withdrawn_command.add_argument('--json', action='store_true')
    source_command = commands.add_parser('set-source', help='补登记来源并重新校准原始版本')
    source_command.add_argument('skill')
    source_command.add_argument('--repo', required=True)
    source_command.add_argument('--path', required=True)
    source_command.add_argument('--yes', action='store_true')
    source_command.add_argument('--json', action='store_true')
    exporting = commands.add_parser('export-config', help='导出可分享的技能清单和分配')
    exporting.add_argument('--output', default='skill-pilot-config.json', help='保存位置，不覆盖已有文件')
    exporting.add_argument('--agent', help='只导出这个 agent 在用的技能，默认全部在用技能')
    exporting.add_argument('--json', action='store_true')
    importing = commands.add_parser('import-config', help='预览或批量导入分享的技能配置')
    importing.add_argument('file')
    importing.add_argument('--agent', action='append', help='改为分配给本机的这些 agent')
    importing.add_argument('--yes', action='store_true')
    importing.add_argument('--json', action='store_true')
    undo = commands.add_parser("restore", help="确认还原到指定备份")
    undo.add_argument("backup_id")
    undo.add_argument("--json", action="store_true")
    local_update = commands.add_parser('update-local', help='用本地文件夹更新一个自制技能')
    local_update.add_argument('skill')
    local_update.add_argument('folder')
    local_update.add_argument('--yes', action='store_true')
    local_update.add_argument('--json', action='store_true')
    adding = commands.add_parser('add-agent', help='按文件夹接入一个 agent')
    adding.add_argument('--name', required=True, help='显示名字')
    adding.add_argument('folder', help='这个 agent 的技能文件夹')
    adding.add_argument('--yes', action='store_true')
    adding.add_argument('--json', action='store_true')
    watching = commands.add_parser('watch-updates', help='设置某个技能检查更新时怎么提醒')
    watching.add_argument('skill')
    watching.add_argument('--mode', choices=['all', 'description', 'off'], required=True)
    watching.add_argument('--yes', action='store_true')
    watching.add_argument('--json', action='store_true')
    extras = commands.add_parser('extra-sources', help='查看或记下主目录里另外放着技能的文件夹')
    extras.add_argument('folder', nargs='?')
    extras.add_argument('--remove', action='store_true', help='不再额外查看这个文件夹')
    extras.add_argument('--json', action='store_true')
    checking = commands.add_parser('check-updates', help='只读检查技能更新')
    checking.add_argument('--json', action='store_true')
    updating = commands.add_parser('update', help='核对并更新选中的技能')
    updating.add_argument('skills', nargs='+')
    updating.add_argument('--yes', action='store_true')
    updating.add_argument('--choice', choices=['keep', 'upstream', 'merge'])
    updating.add_argument('--review', help='已生成的合并预览编号')
    updating.add_argument('--merge-id', action='append', default=[], help='已核对合并的 技能=编号')
    updating.add_argument('--confirm-lark-cli', action='store_true', help='单独确认同时升级飞书工具')
    updating.add_argument('--json', action='store_true')
    for name in ('status', 'list', 'assign', 'unassign', 'absorb', 'install', 'log', 'undo'):
        command = commands.add_parser(name)
        command.add_argument('--json', action='store_true')
        if name in {'list', 'assign', 'unassign'}:
            command.add_argument('--agent', required=True)
        if name in {'assign', 'unassign'}:
            command.add_argument('skill')
            command.add_argument('--yes', action='store_true')
        if name == 'absorb':
            command.add_argument('--all', action='store_true')
            command.add_argument('--yes', action='store_true')
        if name == 'install':
            command.add_argument('github_url')
            command.add_argument('--skill', action='append')
            command.add_argument('--agent', action='append', default=[])
            command.add_argument('--yes', action='store_true')
        if name == 'undo':
            command.add_argument('id')
    args = parser.parse_args()
    if args.command == 'hub':
        from . import team_hub
        try:
            team_hub.run(args)
        except ValueError as error:
            print(str(error))
            raise SystemExit(1)
        except (OSError, OverflowError):
            print('分享站没能启动或读写数据，请检查数据文件夹权限、监听地址和端口。')
            raise SystemExit(1)
        return
    os.environ['PATH'] = tool_environment(target_home())['PATH']
    if args.command == 'cleanup':
        if args.list and (args.skills or args.yes):
            parser.error('--list 只查看已撤下的技能，不能同时指定技能或 --yes。')
        if not args.list and not args.skills:
            parser.error('请指定要撤下的技能，或用 --list 查看已撤下的技能。')
    def fail(error):
        print(json.dumps({'error': str(error)}, ensure_ascii=False) if args.json else str(error))
        raise SystemExit(1)

    def attempt(work):
        try:
            result = work()
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result['message'])
        except (ValueError, OSError, KeyError, TypeError) as error:
            fail(error)
    if args.command == 'extra-sources':
        from .extra_sources import add, ensure, paths, remove
        home = target_home()
        try:
            ensure(home)
            if args.remove and not args.folder:
                raise ValueError('请填写要不再查看的文件夹。')
            if args.folder:
                message = remove(home, args.folder) if args.remove else add(home, args.folder)
            else:
                listed = paths(home)
                message = '还没有额外的技能源。' if not listed else '额外查看这些文件夹：\n' + '\n'.join(listed)
            result = {'message': message, 'paths': paths(home)}
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result['message'])
        except (ValueError, OSError) as error:
            fail(error)
        return
    if args.command in {'export-config', 'import-config'}:
        from . import sharing
        try:
            if args.command == 'export-config':
                config = sharing.export_config(agent=args.agent)
                path = Path(args.output).expanduser().absolute()
                try:
                    with path.open('x', encoding='utf-8') as stream:
                        stream.write(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
                except FileExistsError:
                    raise ValueError('保存位置已有文件，请换一个文件名。') from None
                result = {'path': str(path), 'count': len(config['skills']),
                          'message': f'已导出 {len(config["skills"])} 个技能的配置；把文件交给对方的 agent 即可。'}
            else:
                config = sharing.read_config(args.file)
                result = (sharing.import_config(config, args.agent) if args.yes else
                          sharing.preview_import(config, args.agent))
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(result['message'])
                if 'path' in result:
                    print(result['path'])
                for item in result.get('skipped', []):
                    print(item['name'] + '：' + item['reason'])
                if result.get('unmapped_agents'):
                    print('本机还没接入：' + '、'.join(result['unmapped_agents']) + '；可用 --agent 指定接收方。')
                if args.command == 'import-config' and not args.yes:
                    print('确认安装和分配时，在同一命令后加 --yes；已有技能内容会保留。')
                if result.get('id'):
                    print('可以撤销：skill-pilot undo ' + result['id'])
            if args.command == 'import-config' and args.yes and not result['complete']:
                raise SystemExit(2)
        except (ValueError, OSError, KeyError, TypeError) as error:
            fail(error)
        return
    if args.command in {'cleanup','restore-withdrawn','set-source'}:
        from . import cleanup
        try:
            if args.command == 'cleanup':
                result = (cleanup.list_withdrawn() if args.list else
                          cleanup.apply(args.skills, confirmed=True) if args.yes else cleanup.preview(args.skills))
            elif args.command == 'restore-withdrawn':
                result = cleanup.restore(args.skills)
            else:
                result = operations.set_source(args.skill,args.repo,args.path,args.yes)
            if args.json:
                print(json.dumps(result,ensure_ascii=False,indent=2))
            elif args.command == 'cleanup' and args.list:
                print('已撤下：'+'、'.join(r['name'] for r in result['withdrawn']) if result['withdrawn'] else '还没有已撤下的技能。')
            else:
                print(result['message'])
                for line in result.get('dependency_warnings', []):
                    print(line)
                for warning in result.get('path_warnings',[]):
                    print('受影响的文件：'+warning['file'])
                if args.command == 'cleanup' and not args.yes:
                    print('请核对上述影响，加 --yes 执行。')
            return
        except (ValueError,OSError,KeyError,TypeError) as error:
            fail(error)
    if args.command == 'update-local':
        attempt(lambda: operations.update_local(args.skill, args.folder, confirmed=args.yes))
        return
    if args.command == 'add-agent':
        from . import agents as agent_management
        def work():
            request = [{'name': args.name, 'skill_root': args.folder}]
            plan = agent_management.preview_join(request)
            if args.yes:
                return agent_management.join(request, plan['plan_id'])
            count = len(plan['skills'])
            names = '、'.join(p['name'] for p in plan['profiles'])
            return {'message': f'将接入「{names}」，会登记 {count} 个技能。请核对后加 --yes 执行。',
                    'agents': plan['agents'], 'skills': [skill['name'] for skill in plan['skills']]}
        attempt(work)
        return
    if args.command == 'watch-updates':
        from . import updates
        def work():
            if not args.yes:
                raise ValueError('请先确认，再加 --yes 保存这个提醒方式。')
            return updates.set_watch(args.skill, args.mode, confirmed=True)
        attempt(work)
        return
    if args.command in {'check-updates', 'update'}:
        from . import updates
        review = None
        keep_review = bool(args.command == 'update' and args.review)
        try:
            if args.command == 'update' and not args.yes:
                raise ValueError('更新需要用户明确同意，请先检查，再加 --yes 执行。')
            if args.command == 'update' and args.review:
                import re
                if not re.fullmatch(r'skill-pilot-updates-[A-Za-z0-9_-]+', args.review):
                    raise ValueError('合并预览编号不正确。')
                directory = Path(tempfile.gettempdir()) / args.review
                review = json.loads((directory / 'review.json').read_text())
                if review['home'] != str(target_home()) or Path(review['temporary']).resolve() != directory.resolve():
                    raise ValueError('这份合并预览不属于当前目录。')
            else:
                review = updates.check()
            if args.command == 'check-updates':
                result = updates.public(review)
            else:
                choice = args.choice or ('merge' if args.merge_id else None)
                choices = {name: choice for name in args.skills} if choice else {}
                if args.choice == 'merge' and not args.review:
                    previews = [updates.prepare_merge(review, name) for name in args.skills]
                    result = {'needs_confirmation': True, 'review_id': review['id'], 'merges': previews,
                              'message': '合并结果已生成，文件未改动。看过对比后，用 --review 和 --merge-id 再确认。'}
                    (Path(review['temporary']) / 'review.json').write_text(json.dumps(review, ensure_ascii=False))
                    keep_review = True
                else:
                    result = updates.apply(review, args.skills, choices, True,
                                           dict(item.split('=', 1) for item in args.merge_id), args.confirm_lark_cli)
                    keep_review = False
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            elif args.command == 'check-updates':
                print(f'发现 {result["update_count"]} 个技能有更新。')
                for row in result['skills']:
                    if row['has_update']:
                        print(f'{row["name"]}：{row.get("updated_at", "")}，{row["file_count"]} 个文件，'
                              f'新增 {row["added_lines"]} 行，删除 {row["removed_lines"]} 行' +
                              ('；你改过' if row['local_modified'] else '；本机没改过') +
                              ('；脚本或执行权限有变化，请先看' if row['script_files'] else ''))
                if result['lark']:
                    print(result['lark']['message'])
                for error in result['errors']:
                    print(error['name'] + '：' + error['error'])
            else:
                print(result['message'])
                for failed in result.get('failed', []):
                    print(failed['name'] + '：' + failed['error'])
                for merge in result.get('merges', []):
                    print(merge['name'] + '：' + merge['explanation'] + '\n' + merge['comparison'])
                    print('--review ' + result['review_id'] + ' --merge-id ' + merge['name'] + '=' + merge['merge_id'])
            if result.get('needs_confirmation'):
                raise SystemExit(2)
            if args.command == 'check-updates' and result['errors']:
                raise SystemExit(1)
            if args.command == 'update' and result.get('failed'):
                raise SystemExit(1)
        except (ValueError, OSError, KeyError, TypeError) as error:
            fail(error)
        finally:
            if review and not keep_review:
                shutil.rmtree(review['temporary'], ignore_errors=True)
        return
    if args.command in {'status', 'list', 'assign', 'unassign', 'absorb', 'install', 'log', 'undo'}:
        try:
            if args.command in {'status', 'list'}:
                data = operations.table()
                if args.command == 'list':
                    if args.agent not in {a['id'] for a in data['agents']}:
                        raise ValueError('这个 agent 还没有接入。')
                    data = {'agent': args.agent, 'skills': [s for s in data['skills'] if args.agent in s['assigned_agents']]}
            elif args.command in {'assign', 'unassign'}:
                data = operations.apply_assign([{'skill': args.skill, 'agent': args.agent,
                                                  'enabled': args.command == 'assign'}], confirmed=args.yes)
            elif args.command == 'absorb':
                if not args.yes:
                    raise ValueError('收编会移动新装技能，请核对后加 --yes 执行。')
                data = operations.absorb(all_skills=args.all)
            elif args.command == 'install':
                if not args.yes:
                    raise ValueError('安装会把所选技能收进仓库，请核对后加 --yes 执行。')
                from .sources.github import inspect
                review = inspect(args.github_url)
                try:
                    names = args.skill or [s['name'] for s in review['skills']]
                    data = operations.install(review, names, args.agent)
                    if data.get('conflicts'):
                        from .web.server import compare_files
                        for conflict in data['conflicts']:
                            conflict['comparison'] = compare_files(Path(conflict['existing']), Path(conflict['incoming']))
                finally:
                    shutil.rmtree(review['temporary'])
            elif args.command == 'log':
                data = operations.history()
            else:
                data = operations.undo(args.id)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            elif args.command == 'status':
                for agent in data['agents']:
                    print(f'{agent["name"]}：{agent["count"]} 个技能，约 {agent["description_tokens"]:,} Token；{agent["activation"]}')
                print(f'发现 {len(data["new_skills"])} 个新装的技能。')
            elif args.command == 'list':
                for skill in data['skills']:
                    print(skill['name'] + '：' + skill['description'])
            elif args.command == 'log':
                for record in data:
                    print(f'{record["time"]} {record["message"]}（{"、".join(record["agents"])}） 编号 {record["id"]}' + (' 已撤销' if record['undone'] else ''))
            else:
                print(data['message'])
                for line in data.get('reasons', []) + data.get('dependency_warnings', []):
                    print(line)
                for warning in data.get('path_warnings', []):
                    print('受影响的文件：' + warning['file'])
                if data.get('needs_confirmation'):
                    print('请核对上述影响，加 --yes 执行。')
                for conflict in data.get('conflicts', []):
                    print(conflict['name'] + '：' + conflict['message'])
                    if conflict.get('comparison'):
                        print(conflict['comparison'])
            if isinstance(data, dict) and (data.get('needs_confirmation') or data.get('conflicts')):
                raise SystemExit(2)
            return
        except (ValueError, OSError, KeyError, TypeError) as error:
            fail(error)
    if args.command == "scan":
        data = scan()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print("本次只读取文件。" + data["count_basis"])
            for agent in data["agents"]:
                if agent["visible_skill_count"] is None:
                    state = {"ignored": "已忽略", "discovered": "已发现，尚未接入"}.get(agent["status"], "可见数量暂时无法确认")
                    print(f'{agent["name"]}：{state}')
                else:
                    print(f'{agent["name"]}：{agent["visible_skill_count"]} 个，约 {agent["description_tokens"]:,} Token')
            print(f'发现 {len(data["conflicts"])} 组同名不同内容的技能。')
    elif args.command == "ui":
        from .web.server import run
        run(json_output=args.json)
    else:
        try:
            if args.command == "migrate":
                if args.apply and not args.plan_id:
                    parser.error("执行整理需要先预览，并提供 --plan-id。")
                review = json.loads(open(args.choices).read()) if args.choices else {}
                if not isinstance(review, dict):
                    raise ValueError("核对结果的格式不对，请使用含 choices 和 sources 的对象。")
                data = (preview(agent_ids=args.agents) if args.plan else
                        apply(args.plan_id, agent_ids=args.agents, choices=review.get("choices"), sources=review.get("sources")))
            elif args.command == "backups":
                data = backups()
            else:
                data = restore(args.backup_id)
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=2))
            elif args.command == "migrate" and args.plan:
                print(f"整理预览：{len(data['skills'])} 个技能，{len(data['conflicts'])} 组版本差异。")
                print("要接入的 agent：" + "、".join(data["agents"]))
                for skill in data["conflicts"]:
                    option = next(o for o in skill["options"] if o["id"] == skill["default"])
                    print(f"{skill['name']}：{option['path']}；{skill['reason']}")
                for agent, before in data["before"].items():
                    print(f"{agent}：可调用 {len(before['available'])} → {len(data['after'][agent]['available'])} 个")
                print(f"预览编号：{data['plan_id']}。文件未改动；确认执行需带上这个编号。")
            elif args.command == "backups":
                for item in data:
                    print(item["backup_id"] + "：整理前的备份")
                if not data:
                    print("还没有整理备份。")
            else:
                print(data["message"])
        except (ValueError, OSError) as error:
            fail(error)
