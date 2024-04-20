#!/usr/bin/env python3

# Copyright (c) 2012-2024 Yuri K. Schlesner, CensoredUsername, Jackmcbarn
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


__title__ = "Unrpyc"
__version__ = 'v2.0.2.dev'
__url__ = "https://github.com/CensoredUsername/unrpyc"


import argparse
import glob
import struct
import sys
import traceback
import zlib
from pathlib import Path

try:
    from multiprocessing import Pool, cpu_count
except ImportError:
    # Mock required support when multiprocessing is unavailable
    def cpu_count():
        return 1

import decompiler
import deobfuscate
from decompiler import astdump, translate
from decompiler.renpycompat import (pickle_safe_loads, pickle_safe_dumps, pickle_safe_dump,
                                    pickle_loads, pickle_detect_python2)


class Context:
    def __init__(self):
        self.log_contents = []
        self.state = None
        self.value = None

    def log(self, message):
        self.log_contents.append(message)

    def set_state(self, state):
        self.state = state

    def set_result(self, value):
        self.value = value


class BadRpycException(Exception):
    """Exception raised when we couldn't parse the rpyc archive format"""
    pass


# API

def read_ast_from_file(in_file, context):
    # Reads rpyc v1 or v2 file
    # v1 files are just a zlib compressed pickle blob containing some data and the ast
    # v2 files contain a basic archive structure that can be parsed to find the same blob
    raw_contents = in_file.read()
    l1_start = raw_contents[:50]
    is_rpyc_v1 = False

    if not raw_contents.startswith(b"RENPY RPC2"):
        # if the header isn't present, it should be a RPYC V1 file, which is just the blob
        contents = raw_contents
        is_rpyc_v1 = True

    else:
        # parse the archive structure
        position = 10
        chunks = {}
        have_errored = False

        for expected_slot in range(1, 0xFFFFFFFF):
            slot, start, length = struct.unpack("III", raw_contents[position: position + 12])

            if slot == 0:
                break

            if slot != expected_slot and not have_errored:
                have_errored = True

                context.log(
                    "Warning: Encountered an unexpected slot structure. It is possible the \n"
                    "    file header structure has been changed.")

            position += 12

            chunks[slot] = raw_contents[start: start + length]

        if 1 not in chunks:
            raise BadRpycException(
                "Unable to find the right slot to load from the rpyc file. The file header "
                "structure has been changed."
                f"File header:{l1_start}")

        contents = chunks[1]

    try:
        contents = zlib.decompress(contents)
    except Exception:
        raise BadRpycException(
            "Did not find a zlib compressed blob where it was expected. Either the header has been "
            "modified or the file structure has been changed.") from None

    # add some detection of ren'py 7 files
    if is_rpyc_v1 or pickle_detect_python2(contents):
        version = "6" if is_rpyc_v1 else "7"

        context.log(
            "Warning: analysis found signs that this .rpyc file was generated by ren'py \n"
           f'    version {version} or below, while this unrpyc version targets ren\'py \n'
            "    version 8. Decompilation will still be attempted, but errors or incorrect \n"
            "    decompilation might occur. ")

    _, stmts = pickle_safe_loads(contents)
    return stmts


def decompile_rpyc(input_filename, context, overwrite=False, try_harder=False, dump=False,
                   comparable=False, no_pyexpr=False, translator=None, init_offset=False,
                   sl_custom_names=None):

    # Output filename is input filename but with .rpy extension
    if dump:
        ext = '.txt'
    elif input_filename.suffix == ('.rpyc'):
        ext = '.rpy'
    elif input_filename.suffix == ('.rpymc'):
        ext = '.rpym'
    out_filename = input_filename.with_suffix(ext)

    context.log(f'Decompiling {input_filename} to {out_filename.name}...')

    if not overwrite and out_filename.exists():
        context.log("Target file exists already! Skipping.")
        context.set_state('skip')
        return  # Don't stop decompiling if a file already exists

    with input_filename.open('rb') as in_file:
        if try_harder:
            ast = deobfuscate.read_ast(in_file, context)
        else:
            ast = read_ast_from_file(in_file, context)

    with out_filename.open('w', encoding='utf-8') as out_file:
        if dump:
            astdump.pprint(out_file, ast, comparable=comparable, no_pyexpr=no_pyexpr)
        else:
            options = decompiler.Options(log=context.log_contents, translator=translator,
                                         init_offset=init_offset, sl_custom_names=sl_custom_names)

            decompiler.pprint(out_file, ast, options)

    context.set_state('ok')

def extract_translations(input_filename, language, context):
    context.log(f'Extracting translations from {input_filename}...')

    with input_filename.open('rb') as in_file:
        ast = read_ast_from_file(in_file)

    translator = translate.Translator(language, True)
    translator.translate_dialogue(ast)
    # we pickle and unpickle this manually because the regular unpickler will choke on it
    return pickle_safe_dumps(translator.dialogue), translator.strings


def worker(arg_tup):
    args, filename = arg_tup
    context = Context()

    try:
        if args.write_translation_file:
            result = extract_translations(filename, args.language, context)
        else:
            if args.translation_file is not None:
                translator = translate.Translator(None)
                translator.language, translator.dialogue, translator.strings = (
                    pickle_loads(args.translations))
            else:
                translator = None
            result = decompile_rpyc(
                filename, context, args.clobber, try_harder=args.try_harder, dump=args.dump,
                no_pyexpr=args.no_pyexpr, comparable=args.comparable, translator=translator,
                init_offset=args.init_offset, sl_custom_names=args.sl_custom_names
                )

        context.set_result(result)

    except BadRpycException:
        context.set_state('spoofed')
        context.log(f'Error while trying to read the header of {filename}:')
        context.log(traceback.format_exc())
    except Exception:
        context.set_state('fail')
        context.log(f'Error while decompiling {filename}:')
        context.log(traceback.format_exc())

    return context


def parse_sl_custom_names(unparsed_arguments):
    # parse a list of strings in the format
    # classname=name-nchildren into {classname: (name, nchildren)}
    parsed_arguments = {}
    for argument in unparsed_arguments:
        content = argument.split("=")
        if len(content) != 2:
            raise Exception(f'Bad format in custom sl displayable registration: "{argument}"')

        classname, name = content
        split = name.split("-")
        if len(split) == 1:
            amount = "many"

        elif len(split) == 2:
            name, amount = split
            if amount == "0":
                amount = 0
            elif amount == "1":
                amount = 1
            elif amount == "many":
                pass
            else:
                raise Exception(
                    f'Bad child node count in custom sl displayable registration: "{argument}"')

        else:
            raise Exception(
                f'Bad format in custom sl displayable registration: "{argument}"')

        parsed_arguments[classname] = (name, amount)

    return parsed_arguments


def main():
    if not sys.version_info[:2] >= (3, 9):
        raise Exception(
            f"'{__title__} {__version__}' must be executed with Python 3.9 or later.\n"
            f"You are running {sys.version}")

    # argparse usage: python3 unrpyc.py [-c] [--try-harder] [-d] [-p] file [file ...]
    cc_num = cpu_count()
    ap = argparse.ArgumentParser(description="Decompile .rpyc/.rpymc files")

    ap.add_argument(
        'file',
        type=str,
        nargs='+',
        help="The filenames to decompile. "
        "All .rpyc files in any sub-/directories passed will also be decompiled.")

    ap.add_argument(
        '-c',
        '--clobber',
        dest='clobber',
        action='store_true',
        help="Overwrites output files if they already exist.")

    ap.add_argument(
        '--try-harder',
        dest="try_harder",
        action="store_true",
        help="Tries some workarounds against common obfuscation methods. This is a lot slower.")

    ap.add_argument(
        '-d',
        '--dump',
        dest='dump',
        action='store_true',
        help="Instead of decompiling, pretty print the ast to a file")

    ap.add_argument(
        '-p',
        '--processes',
        dest='processes',
        action='store',
        type=int,
        choices=list(range(1, cc_num)),
        default=cc_num - 1 if cc_num > 2 else 1,
        help="Use the specified number or processes to decompile. "
        "Defaults to the amount of hw threads available minus one, disabled when muliprocessing "
        "unavailable is.")

    ap.add_argument(
        '-t',
        '--translation-file',
        dest='translation_file',
        type=Path,
        action='store',
        default=None,
        help="Use the specified file to translate during decompilation")

    ap.add_argument(
        '-T',
        '--write-translation-file',
        dest='write_translation_file',
        type=Path,
        action='store',
        default=None,
        help="Store translations in the specified file instead of decompiling")

    ap.add_argument(
        '-l',
        '--language',
        dest='language',
        action='store',
        default=None,
        help="If writing a translation file, the language of the translations to write")

    ap.add_argument(
        '--comparable',
        dest='comparable',
        action='store_true',
        help="Only for dumping, remove several false differences when comparing dumps. "
        "This suppresses attributes that are different even when the code is identical, such as "
        "file modification times. ")

    ap.add_argument(
        '--no-pyexpr',
        dest='no_pyexpr',
        action='store_true',
        help="Only for dumping, disable special handling of PyExpr objects, instead printing them "
        "as strings. This is useful when comparing dumps from different versions of Ren'Py. It "
        "should only be used if necessary, since it will cause loss of information such as line "
        "numbers.")

    ap.add_argument(
        '--no-init-offset',
        dest='init_offset',
        action='store_false',
        help="By default, unrpyc attempt to guess when init offset statements were used and insert "
        "them. This is always safe to do for ren'py 8, but as it is based on a heuristic it can be "
        "disabled. The generated code is exactly equivalent, only slightly more cluttered.")

    ap.add_argument(
        '--register-sl-displayable',
        dest="sl_custom_names",
        type=str,
        nargs='+',
        help="Accepts mapping separated by '=', "
        "where the first argument is the name of the user-defined displayable object, "
        "and the second argument is a string containing the name of the displayable, "
        "potentially followed by a '-', and the amount of children the displayable takes"
        "(valid options are '0', '1' or 'many', with 'many' being the default)")

    ap.add_argument(
        '--version',
        action='version',
        version=f"{__title__} {__version__}")

    args = ap.parse_args()
    # Basic start state
    state_count = dict({'total': 0, 'ok': 0, 'fail': 0, 'skip': 0, 'spoofed': 0})

    # Catch impossible arg combinations so they don't produce strange errors or fail silently
    if (args.no_pyexpr or args.comparable) and not args.dump:
        raise ap.error(
            "Arguments 'comparable' and 'no_pyexpr' are not usable without 'dump'.")

    if ((args.try_harder or args.dump)
            and (args.write_translation_file or args.translation_file or args.language)):
        raise ap.error(
            "Arguments 'try_harder' and/or 'dump' are not usable with the translation "
            "feature.")

    # Fail early to avoid wasting time going through the files
    if (args.write_translation_file
            and not args.clobber
            and args.write_translation_file.exists()):
        raise ap.error(
            "Output translation file already exists. Pass --clobber to overwrite.")

    if args.translation_file:
        with args.translation_file.open('rb') as in_file:
            args.translations = in_file.read()

    if args.sl_custom_names is not None:
        try:
            args.sl_custom_names = parse_sl_custom_names(args.sl_custom_names)
        except Exception as e:
            print("\n".join(e.args))
            return

    def glob_or_complain(inpath):
        """Expands wildcards and casts output to pathlike state."""
        retval = [Path(elem).resolve(strict=True) for elem in glob.glob(inpath, recursive=True)]
        if not retval:
            print(f'Input path not found: {inpath}')
        return retval

    def traverse(inpath):
        """
        Filters from input path for rpyc/rpymc files and returns them. Recurses into all given
        directories by calling itself.
        """
        if inpath.is_file() and inpath.suffix in ['.rpyc', '.rpymc']:
            yield inpath
        elif inpath.is_dir():
            for item in inpath.iterdir():
                yield from traverse(item)

    # Check paths from argparse through globing and pathlib. Constructs a tasklist with all
    # `Ren'Py compiled files` the app was assigned to process.
    worklist = []
    for entry in args.file:
        for globitem in glob_or_complain(entry):
            for elem in traverse(globitem):
                worklist.append(elem)

    # Check if we actually have files. Don't worry about no parameters passed,
    # since ArgumentParser catches that
    if not worklist:
        print("Found no script files to decompile.")
        return
    state_count['total'] = len(worklist)

    # If a big file starts near the end, there could be a long time with only one thread running,
    # which is inefficient. Avoid this by starting big files first.
    worklist.sort(key=lambda x: x.stat().st_size, reverse=True)
    worklist = [(args, x) for x in worklist]

    results = []
    if args.processes > 1 and len(worklist) > 5:
        with Pool(args.processes) as pool:
            for result in pool.imap(worker, worklist, 1):
                results.append(result)
    else:
        for result in map(worker, worklist):
            results.append(result)

    if args.write_translation_file:
        print(f'Writing translations to {args.write_translation_file}...')
        translated_dialogue = {}
        translated_strings = {}
        for result in results:
            if not result.value:
                continue
            translated_dialogue.update(pickle_loads(result.value[0]))
            translated_strings.update(result.value[1])
        with args.write_translation_file.open('wb') as out_file:
            pickle_safe_dump((args.language, translated_dialogue, translated_strings), out_file)

    # Get infos per instance and write them to their targets
    for res_inst in results:
        state_count[res_inst.state] += 1

        for log_entry in res_inst.log_contents:
            print(log_entry)

    def plural_fmt(inp):
        """returns singular or plural of term file(s) contingent of input count"""
        return f"{inp} file{'s'[:inp^1]}"

    endreport = (
        "\nThis decompile run of Unrpyc has the following outcome:\n"
        f"{55 * '-'}\n"
        f"  A total of {plural_fmt(state_count['total'])} to decompile where found.\n"
        f"  > {plural_fmt(state_count['ok'])} could be successful decompiled.\n"
        f"  > {plural_fmt(state_count['fail'])} failed due to diverse errors.\n"
        f"  > {plural_fmt(state_count['spoofed'])} with wrong header or other manipulation.\n"
        f"  > {plural_fmt(state_count['skip'])} already exist and have been skipped.\n"
    )
    # add pointers if we encounter problems
    skipped = ("To overwrite existing files use option '--clobber'. "
               if state_count['skip'] != 0 else "")
    spoofed = (
        "In case of manipulation, the --try-harder option can be attempted."
        if state_count['spoofed'] != 0 else "")
    errors = ("Errors were found. Check the exceptions in the log for more info about this."
        if state_count['fail'] != 0 else "")
    print(endreport, skipped, spoofed, errors)

if __name__ == '__main__':
    main()
