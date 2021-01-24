"""
Transformers are classes used to transform passed Robot Framework code model.
You can create your own transformer class if you follow those rules:
    - inherit from `ModelTransformer` class
    - add `@transformer` class decorator

Classes that do not met all of those two conditions will not be loaded into `robotidy` as transformers.
Thanks for that you can use it to create common classes / helper methods:

    class NotATransformer(ModelTransformer):
        pass

"""
import inspect
import re
import sys
import ast
from collections import Counter

import click
from robot.api.parsing import (
    ModelTransformer,
    Token,
    Variable,
    EmptyLine,
    Comment,
    KeywordCall,
    CommentSection,
    If,
    End,
    IfHeader,
    ElseHeader,
    ElseIfHeader
)
from robot.utils.importer import Importer


from robotidy.decorators import (
    transformer,
    check_start_end_line,
    TRANSFORMERS
)
from robotidy.utils import normalize_name


def load_transformers(allowed_transformers):
    """ Dynamically load all classes from this file with attribute `name` defined in allowed_transformers """
    if allowed_transformers:
        loaded_transformers = dict()
        for name, args in allowed_transformers:
            name = f'robotidy.transformers.{name}' if name in TRANSFORMERS else name
            loaded_transformers[name] = Importer().import_class_or_module(
                name,
                instantiate_with_args=args
            )
        return loaded_transformers
    else:
        return {name: Importer().import_class_or_module(f'robotidy.transformers.{name}', instantiate_with_args=())
                for name in TRANSFORMERS}


@transformer
class DiscardEmptySections(ModelTransformer):
    """
    Remove empty sections. Sections are considered empty if there is no data or there are only comments inside
    (with the exception for *** Comments *** section).
    You can leave sections with only comments by configuring `allow_only_comments` to True.
    Supports global formatting params: `--startline` and `--endline`
    """
    def __init__(self, allow_only_comments: bool = False):
        # If True then sections only with comments are not considered as empty
        self.allow_only_comments = allow_only_comments

    @check_start_end_line
    def check_if_empty(self, node):
        anything_but = EmptyLine if self.allow_only_comments or isinstance(node, CommentSection) else (Comment, EmptyLine)
        if all(isinstance(child, anything_but) for child in node.body):
            return None
        return node

    def visit_SettingSection(self, node):  # noqa
        return self.check_if_empty(node)

    def visit_VariableSection(self, node):  # noqa
        return self.check_if_empty(node)

    def visit_TestCaseSection(self, node):  # noqa
        return self.check_if_empty(node)

    def visit_KeywordSection(self, node):  # noqa
        return self.check_if_empty(node)

    def visit_CommentSection(self, node):  # noqa
        return self.check_if_empty(node)


def insert_separators(indent, tokens, formatting_config):
    yield Token(Token.SEPARATOR, indent + formatting_config.space_count * ' ')
    for token in tokens[:-1]:
        yield token
        yield Token(Token.SEPARATOR, formatting_config.space_count * ' ')
    yield tokens[-1]
    yield Token(Token.EOL)


@transformer
class ReplaceRunKeywordIf(ModelTransformer):
    """
    Replace `Run Keyword If` keyword calls with IF END blocks.
    Supports global formatting params: `--spacecount`, `--startline` and `--endline`.

    Following code::

        Run Keyword If  ${condition}
        ...  Keyword  ${arg}
        ...  ELSE IF  ${condition2}  Keyword2
        ...  ELSE  Keyword3

    Will be transformed to::

        IF    ${condition}
            Keyword    ${arg}
        ELSE IF    ${condition2}
            Keyword2
        ELSE
            Keyword3
        END

    Any return value will be applied to every ELSE/ELSE IF branch::

        ${var}  Run Keyword If  ${condition}  Keyword  ELSE  Keyword2

    Output::

        IF    ${condition}
            ${var}    Keyword
        ELSE
            ${var}    Keyword2
        END

    Run Keywords inside Run Keyword If will be splitted into separate keywords::

       Run Keyword If  ${condition}  Run Keywords  Keyword  ${arg}  AND  Keyword2

    Output::

        IF    ${condition}
            Keyword    ${arg}
            Keyword2
        END

    """
    @check_start_end_line
    def visit_KeywordCall(self, node):  # noqa
        if not node.keyword:
            return node
        if normalize_name(node.keyword) == 'runkeywordif':
            return self.create_branched(node)
        return node

    def create_branched(self, node):
        separator = node.tokens[0]
        assign = node.get_tokens(Token.ASSIGN)
        raw_args = node.get_tokens(Token.ARGUMENT)
        if len(raw_args) < 2:
            return node
        end = End([
            separator,
            Token(Token.END),
            Token(Token.EOL)
        ])
        prev_if = None
        for branch in reversed(list(self.split_args_on_delimeters(raw_args, ('ELSE', 'ELSE IF')))):
            if branch[0].value == 'ELSE':
                header = ElseHeader([
                    separator,
                    Token(Token.ELSE),
                    Token(Token.EOL)
                ])
                if len(branch) < 2:
                    return node
                args = branch[1:]
            elif branch[0].value == 'ELSE IF':
                if len(branch) < 3:
                    return node
                header = ElseIfHeader([
                    separator,
                    Token(Token.ELSE_IF),
                    Token(Token.SEPARATOR, self.formatting_config.space_count * ' '),
                    branch[1],
                    Token(Token.EOL)
                ])
                args = branch[2:]
            else:
                if len(branch) < 2:
                    return node
                header = IfHeader([
                    separator,
                    Token(Token.IF),
                    Token(Token.SEPARATOR, self.formatting_config.space_count * ' '),
                    branch[0],
                    Token(Token.EOL)
                ])
                args = branch[1:]
            keywords = self.create_keywords(args, assign, separator.value)
            if_block = If(header=header, body=keywords, orelse=prev_if)
            prev_if = if_block
        prev_if.end = end
        return prev_if

    def create_keywords(self, arg_tokens, assign, indent):
        if normalize_name(arg_tokens[0].value) == 'runkeywords':
            return [self.args_to_keyword(keyword[1:], assign, indent)
                    for keyword in self.split_args_on_delimeters(arg_tokens, ('AND',))]
        return self.args_to_keyword(arg_tokens, assign, indent)

    def args_to_keyword(self, arg_tokens, assign, indent):
        separated_tokens = list(insert_separators(
            indent,
            [*assign, Token(Token.KEYWORD, arg_tokens[0].value), *arg_tokens[1:]],
            self.formatting_config
        ))
        return KeywordCall.from_tokens(separated_tokens)

    @staticmethod
    def split_args_on_delimeters(args, delimeters):
        split_points = [index for index, arg in enumerate(args) if arg.value in delimeters]
        prev_index = 0
        for split_point in split_points:
            yield args[prev_index:split_point]
            prev_index = split_point
        yield args[prev_index:len(args)]


@transformer
class AssignmentNormalizer(ModelTransformer):
    """
    Normalize assignments. By default it detects most common assignment sign
    and apply it to every assignment in given file.

    In this code most common is no equal sign at all. We should remove `=` signs from the all lines::

        *** Variables ***
        ${var} =  ${1}
        @{list}  a
        ...  b
        ...  c

        ${variable}=  10


        *** Keywords ***
        Keyword
            ${var}  Keyword1
            ${var}   Keyword2
            ${var}=    Keyword

    To::

        *** Variables ***
        ${var}  ${1}
        @{list}  a
        ...  b
        ...  c

        ${variable}  10


        *** Keywords ***
        Keyword
            ${var}  Keyword1
            ${var}   Keyword2
            ${var}    Keyword

    You can configure that behaviour to automatically add desired equal sign with `equal_sign_type` configurable
    (possible types are: `remove`, `equal_sign` ('='), `space_and_equal_sign` (' =').

    """
    def __init__(self, equal_sign_type: str = 'autodetect'):
        self.remove_equal_sign = re.compile(r'\s?=$')
        self.file_equal_sign_type = None
        self.equal_sign_type = self.parse_equal_sign_type(equal_sign_type)

    @staticmethod
    def parse_equal_sign_type(value):
        types = {
            'remove': '',
            'equal_sign': '=',
            'space_and_equal_sign': ' =',
            'autodetect': None
        }
        if value not in types:
            raise click.BadOptionUsage(
                option_name='transform',
                message=f"Invalid configurable value: {value} for equal_sign_type for AssignmentNormalizer transformer."
                        f" Possible values:\n    remove\n    equal_sign\n    space_and_equal_sign"
            )
        return types[value]

    def visit_File(self, node):  # noqa
        """
        If no assignment sign was set the file will be scanned to find most common assignment sign.
        This auto detection will happen for every file separately.
        """
        if self.equal_sign_type is None:
            equal_sign_type = self.auto_detect_equal_sign(node)
            if equal_sign_type is None:
                return node
            self.file_equal_sign_type = equal_sign_type
        self.generic_visit(node)
        self.file_equal_sign_type = None

    def visit_KeywordCall(self, node):  # noqa
        if node.assign:  # if keyword returns any value
            assign_tokens = node.get_tokens(Token.ASSIGN)
            self.normalize_equal_sign(assign_tokens[-1])
        return node

    def visit_VariableSection(self, node):  # noqa
        for child in node.body:
            if not isinstance(child, Variable):
                continue
            var_token = child.get_token(Token.VARIABLE)
            self.normalize_equal_sign(var_token)
        return node

    def normalize_equal_sign(self, token):
        token.value = re.sub(self.remove_equal_sign, '', token.value)
        if self.equal_sign_type:
            token.value += self.equal_sign_type
        elif self.file_equal_sign_type:
            token.value += self.file_equal_sign_type

    @staticmethod
    def auto_detect_equal_sign(node):
        auto_detector = AssignmentTypeDetector()
        auto_detector.visit(node)
        return auto_detector.most_common


class AssignmentTypeDetector(ast.NodeVisitor):
    def __init__(self):
        self.sign_counter = Counter()
        self.most_common = None

    def visit_File(self, node):  # noqa
        self.generic_visit(node)
        if len(self.sign_counter) >= 2:
            self.most_common = self.sign_counter.most_common(1)[0][0]

    def visit_KeywordCall(self, node):  # noqa
        if node.assign:  # if keyword returns any value
            sign = self.get_assignment_sign(node.assign[-1])
            self.sign_counter[sign] += 1

    def visit_VariableSection(self, node):  # noqa
        for child in node.body:
            if not isinstance(child, Variable):
                continue
            var_token = child.get_token(Token.VARIABLE)
            sign = self.get_assignment_sign(var_token.value)
            self.sign_counter[sign] += 1
        return node

    @staticmethod
    def get_assignment_sign(token_value):
        return token_value[token_value.find('}')+1:]


@transformer
class SplitTooLongLine(ModelTransformer):
    def __init__(self, line_length: int = 120, split_only_at_max_length: bool = True):
        self.line_length = line_length
        self.split_only_at_max_length = split_only_at_max_length
        # self.formatting_config.space_count

    def visit_KeywordCall(self, node):  # noqa
        if all(token.end_col_offset <= self.line_length for token in node.tokens[::-1]):
            return node
        if not node.get_tokens(Token.ARGUMENT):  # return if there are no arguments - nothing to split
            return node
        return self.split_every_line(node)

    def split_every_line(self, node):  # noqa
        indent = node.tokens[0]
        separator = Token(Token.SEPARATOR, self.formatting_config.space_count * ' ')
        assign = node.get_tokens(Token.ASSIGN)
        assigned = list(self.insert_seperator(assign, separator))
        keyword = node.get_tokens(Token.KEYWORD)
        tokens = [token for token in node.tokens if token.type in (Token.ARGUMENT, Token.COMMENT)]
        splitted_tokens = [indent] + assigned + keyword
        curr_len = self.len_of_tokens(splitted_tokens)
        for token in tokens:
            if self.split_only_at_max_length:
                next_token_len = self.len_of_tokens([separator, token])
                if (curr_len + next_token_len) < self.line_length:
                    splitted_tokens.append(separator)
                    splitted_tokens.append(token)
                    curr_len += next_token_len
                else:
                    splitted_tokens.extend(self.new_line(indent, separator, token))
                    curr_len = next_token_len + len(indent.value) + 3
            else:
                splitted_tokens.extend(self.new_line(indent, separator, token))

        node.tokens = splitted_tokens + [Token(Token.EOL)]
        return node

    @staticmethod
    def insert_seperator(iterator, separator):
        for elem in iterator:
            yield elem
            yield separator

    @staticmethod
    def new_line(indent, separator, token):
        return [Token(Token.EOL), indent, Token(Token.CONTINUATION), separator, token]

    @staticmethod
    def len_of_tokens(tokens):
        return sum(len(token.value) for token in tokens)
