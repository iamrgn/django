from __future__ import unicode_literals

from django.db import models
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ModelState
from django.db.models.options import normalize_together
from django.utils import six
from django.utils.functional import cached_property

from .fields import (
    AddField, AlterField, FieldOperation, RemoveField, RenameField,
)


def _check_for_duplicates(arg_name, objs):
    used_vals = set()
    for val in objs:
        if val in used_vals:
            raise ValueError(
                "Found duplicate value %s in CreateModel %s argument." % (val, arg_name)
            )
        used_vals.add(val)


class ModelOperation(Operation):
    def __init__(self, name):
        self.name = name

    @cached_property
    def name_lower(self):
        return self.name.lower()

    def references_model(self, name, app_label=None):
        return name.lower() == self.name_lower

    def reduce(self, operation, in_between, app_label=None):
        return (
            super(ModelOperation, self).reduce(operation, in_between, app_label=app_label) or
            not operation.references_model(self.name, app_label)
        )


class CreateModel(ModelOperation):
    """
    Create a model's table.
    """

    serialization_expand_args = ['fields', 'options', 'managers']

    def __init__(self, name, fields, options=None, bases=None, managers=None):
        self.fields = fields
        self.options = options or {}
        self.bases = bases or (models.Model,)
        self.managers = managers or []
        super(CreateModel, self).__init__(name)
        # Sanity-check that there are no duplicated field names, bases, or
        # manager names
        _check_for_duplicates('fields', (name for name, _ in self.fields))
        _check_for_duplicates(
            'bases',
            (base._meta.label_lower if isinstance(base, models.base.ModelBase) else base.lower()
             for base in self.bases
             if base is not models.Model)
        )
        _check_for_duplicates('managers', (name for name, _ in self.managers))

    def deconstruct(self):
        kwargs = {
            'name': self.name,
            'fields': self.fields,
        }
        if self.options:
            kwargs['options'] = self.options
        if self.bases and self.bases != (models.Model,):
            kwargs['bases'] = self.bases
        if self.managers and self.managers != [('objects', models.Manager())]:
            kwargs['managers'] = self.managers
        return (
            self.__class__.__name__,
            [],
            kwargs
        )

    def state_forwards(self, app_label, state):
        state.add_model(ModelState(
            app_label,
            self.name,
            list(self.fields),
            dict(self.options),
            tuple(self.bases),
            list(self.managers),
        ))

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            schema_editor.create_model(model)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(app_label, self.name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            schema_editor.delete_model(model)

    def describe(self):
        return "Create %smodel %s" % ("proxy " if self.options.get("proxy", False) else "", self.name)

    def references_model(self, name, app_label=None):
        strings_to_check = [self.name]
        # Check we didn't inherit from the model
        for base in self.bases:
            if isinstance(base, six.string_types):
                strings_to_check.append(base.split(".")[-1])
        # Check we have no FKs/M2Ms with it
        for fname, field in self.fields:
            if field.remote_field:
                if isinstance(field.remote_field.model, six.string_types):
                    strings_to_check.append(field.remote_field.model.split(".")[-1])
        # Now go over all the strings and compare them
        for string in strings_to_check:
            if string.lower() == name.lower():
                return True
        return False

    def model_to_key(self, model):
        """
        Take either a model class or an "app_label.ModelName" string
        and return (app_label, object_name).
        """
        if isinstance(model, six.string_types):
            return model.split(".", 1)
        else:
            return model._meta.app_label, model._meta.object_name

    def reduce(self, operation, in_between, app_label=None):
        if (isinstance(operation, DeleteModel) and
                self.name_lower == operation.name_lower and
                not self.options.get("proxy", False)):
            return []
        elif isinstance(operation, RenameModel) and self.name_lower == operation.old_name_lower:
            return [
                CreateModel(
                    operation.new_name,
                    fields=self.fields,
                    options=self.options,
                    bases=self.bases,
                    managers=self.managers,
                ),
            ]
        elif isinstance(operation, FieldOperation) and self.name_lower == operation.model_name_lower:
            if isinstance(operation, AddField):
                # Don't allow optimizations of FKs through models they reference
                if hasattr(operation.field, "remote_field") and operation.field.remote_field:
                    for between in in_between:
                        # Check that it doesn't point to the model
                        app_label, object_name = self.model_to_key(operation.field.remote_field.model)
                        if between.references_model(object_name, app_label):
                            return False
                        # Check that it's not through the model
                        if getattr(operation.field.remote_field, "through", None):
                            app_label, object_name = self.model_to_key(operation.field.remote_field.through)
                            if between.references_model(object_name, app_label):
                                return False
                return [
                    CreateModel(
                        self.name,
                        fields=self.fields + [(operation.name, operation.field)],
                        options=self.options,
                        bases=self.bases,
                        managers=self.managers,
                    ),
                ]
            elif isinstance(operation, AlterField):
                return [
                    CreateModel(
                        self.name,
                        fields=[
                            (n, operation.field if n == operation.name else v)
                            for n, v in self.fields
                        ],
                        options=self.options,
                        bases=self.bases,
                        managers=self.managers,
                    ),
                ]
            elif isinstance(operation, RemoveField):
                return [
                    CreateModel(
                        self.name,
                        fields=[
                            (n, v)
                            for n, v in self.fields
                            if n.lower() != operation.name_lower
                        ],
                        options=self.options,
                        bases=self.bases,
                        managers=self.managers,
                    ),
                ]
            elif isinstance(operation, RenameField):
                return [
                    CreateModel(
                        self.name,
                        fields=[
                            (operation.new_name if n == operation.old_name else n, v)
                            for n, v in self.fields
                        ],
                        options=self.options,
                        bases=self.bases,
                        managers=self.managers,
                    ),
                ]
        return super(CreateModel, self).reduce(operation, in_between, app_label=app_label)


class DeleteModel(ModelOperation):
    """
    Drops a model's table.
    """

    def deconstruct(self):
        kwargs = {
            'name': self.name,
        }
        return (
            self.__class__.__name__,
            [],
            kwargs
        )

    def state_forwards(self, app_label, state):
        state.remove_model(app_label, self.name_lower)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = from_state.apps.get_model(app_label, self.name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            schema_editor.delete_model(model)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.name)
        if self.allow_migrate_model(schema_editor.connection.alias, model):
            schema_editor.create_model(model)

    def describe(self):
        return "Delete model %s" % (self.name, )


class RenameModel(ModelOperation):
    """
    Renames a model.
    """

    def __init__(self, old_name, new_name):
        self.old_name = old_name
        self.new_name = new_name
        super(RenameModel, self).__init__(old_name)

    @cached_property
    def old_name_lower(self):
        return self.old_name.lower()

    @cached_property
    def new_name_lower(self):
        return self.new_name.lower()

    def deconstruct(self):
        kwargs = {
            'old_name': self.old_name,
            'new_name': self.new_name,
        }
        return (
            self.__class__.__name__,
            [],
            kwargs
        )

    def state_forwards(self, app_label, state):
        apps = state.apps
        model = apps.get_model(app_label, self.old_name)
        model._meta.apps = apps
        # Get all of the related objects we need to repoint
        all_related_objects = (
            f for f in model._meta.get_fields(include_hidden=True)
            if f.auto_created and not f.concrete and (not f.hidden or f.many_to_many)
        )
        # Rename the model
        state.models[app_label, self.new_name_lower] = state.models[app_label, self.old_name_lower]
        state.models[app_label, self.new_name_lower].name = self.new_name
        state.remove_model(app_label, self.old_name_lower)
        # Repoint the FKs and M2Ms pointing to us
        for related_object in all_related_objects:
            if related_object.model is not model:
                # The model being renamed does not participate in this relation
                # directly. Rather, a superclass does.
                continue
            # Use the new related key for self referential related objects.
            if related_object.related_model == model:
                related_key = (app_label, self.new_name_lower)
            else:
                related_key = (
                    related_object.related_model._meta.app_label,
                    related_object.related_model._meta.model_name,
                )
            new_fields = []
            for name, field in state.models[related_key].fields:
                if name == related_object.field.name:
                    field = field.clone()
                    field.remote_field.model = "%s.%s" % (app_label, self.new_name)
                new_fields.append((name, field))
            state.models[related_key].fields = new_fields
            state.reload_model(*related_key)
        state.reload_model(app_label, self.new_name_lower)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        new_model = to_state.apps.get_model(app_label, self.new_name)
        if self.allow_migrate_model(schema_editor.connection.alias, new_model):
            old_model = from_state.apps.get_model(app_label, self.old_name)
            # Move the main table
            schema_editor.alter_db_table(
                new_model,
                old_model._meta.db_table,
                new_model._meta.db_table,
            )
            # Alter the fields pointing to us
            for related_object in old_model._meta.related_objects:
                if related_object.related_model == old_model:
                    model = new_model
                    related_key = (app_label, self.new_name_lower)
                else:
                    model = related_object.related_model
                    related_key = (
                        related_object.related_model._meta.app_label,
                        related_object.related_model._meta.model_name,
                    )
                to_field = to_state.apps.get_model(
                    *related_key
                )._meta.get_field(related_object.field.name)
                schema_editor.alter_field(
                    model,
                    related_object.field,
                    to_field,
                )
            # Rename M2M fields whose name is based on this model's name.
            fields = zip(old_model._meta.local_many_to_many, new_model._meta.local_many_to_many)
            for (old_field, new_field) in fields:
                # Skip self-referential fields as these are renamed above.
                if new_field.model == new_field.related_model or not new_field.remote_field.through._meta.auto_created:
                    continue
                # Rename the M2M table that's based on this model's name.
                old_m2m_model = old_field.remote_field.through
                new_m2m_model = new_field.remote_field.through
                schema_editor.alter_db_table(
                    new_m2m_model,
                    old_m2m_model._meta.db_table,
                    new_m2m_model._meta.db_table,
                )
                # Rename the column in the M2M table that's based on this
                # model's name.
                schema_editor.alter_field(
                    new_m2m_model,
                    old_m2m_model._meta.get_field(old_model._meta.model_name),
                    new_m2m_model._meta.get_field(new_model._meta.model_name),
                )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self.new_name_lower, self.old_name_lower = self.old_name_lower, self.new_name_lower
        self.new_name, self.old_name = self.old_name, self.new_name

        self.database_forwards(app_label, schema_editor, from_state, to_state)

        self.new_name_lower, self.old_name_lower = self.old_name_lower, self.new_name_lower
        self.new_name, self.old_name = self.old_name, self.new_name

    def references_model(self, name, app_label=None):
        return (
            name.lower() == self.old_name_lower or
            name.lower() == self.new_name_lower
        )

    def describe(self):
        return "Rename model %s to %s" % (self.old_name, self.new_name)

    def reduce(self, operation, in_between, app_label=None):
        if (isinstance(operation, RenameModel) and
                self.new_name_lower == operation.old_name_lower):
            return [
                RenameModel(
                    self.old_name,
                    operation.new_name,
                ),
            ]
        # Skip `ModelOperation.reduce` as we want to run `references_model`
        # against self.new_name.
        return (
            super(ModelOperation, self).reduce(operation, in_between, app_label=app_label) or
            not operation.references_model(self.new_name, app_label)
        )


class AlterModelTable(ModelOperation):
    """
    Renames a model's table
    """

    def __init__(self, name, table):
        self.table = table
        super(AlterModelTable, self).__init__(name)

    def deconstruct(self):
        kwargs = {
            'name': self.name,
            'table': self.table,
        }
        return (
            self.__class__.__name__,
            [],
            kwargs
        )

    def state_forwards(self, app_label, state):
        state.models[app_label, self.name_lower].options["db_table"] = self.table
        state.reload_model(app_label, self.name_lower)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        new_model = to_state.apps.get_model(app_label, self.name)
        if self.allow_migrate_model(schema_editor.connection.alias, new_model):
            old_model = from_state.apps.get_model(app_label, self.name)
            schema_editor.alter_db_table(
                new_model,
                old_model._meta.db_table,
                new_model._meta.db_table,
            )
            # Rename M2M fields whose name is based on this model's db_table
            for (old_field, new_field) in zip(old_model._meta.local_many_to_many, new_model._meta.local_many_to_many):
                if new_field.remote_field.through._meta.auto_created:
                    schema_editor.alter_db_table(
                        new_field.remote_field.through,
                        old_field.remote_field.through._meta.db_table,
                        new_field.remote_field.through._meta.db_table,
                    )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        return self.database_forwards(app_label, schema_editor, from_state, to_state)

    def describe(self):
        return "Rename table for %s to %s" % (self.name, self.table)

    def reduce(self, operation, in_between, app_label=None):
        if isinstance(operation, (AlterModelTable, DeleteModel)) and self.name_lower == operation.name_lower:
            return [operation]
        return super(AlterModelTable, self).reduce(operation, in_between, app_label=app_label)


class ModelOptionOperation(ModelOperation):
    def reduce(self, operation, in_between, app_label=None):
        if isinstance(operation, (self.__class__, DeleteModel)) and self.name_lower == operation.name_lower:
            return [operation]
        return super(ModelOptionOperation, self).reduce(operation, in_between, app_label=app_label)


class FieldRelatedOptionOperation(ModelOptionOperation):
    def reduce(self, operation, in_between, app_label=None):
        if (isinstance(operation, FieldOperation) and
                self.name_lower == operation.model_name_lower and
                not self.references_field(operation.model_name, operation.name)):
            return [operation, self]
        return super(FieldRelatedOptionOperation, self).reduce(operation, in_between, app_label=app_label)


class AlterUniqueTogether(FieldRelatedOptionOperation):
    """
    Changes the value of unique_together to the target one.
    Input value of unique_together must be a set of tuples.
    """
    option_name = "unique_together"

    def __init__(self, name, unique_together):
        unique_together = normalize_together(unique_together)
        self.unique_together = set(tuple(cons) for cons in unique_together)
        super(AlterUniqueTogether, self).__init__(name)

    def deconstruct(self):
        kwargs = {
            'name': self.name,
            'unique_together': self.unique_together,
        }
        return (
            self.__class__.__name__,
            [],
            kwargs
        )

    def state_forwards(self, app_label, state):
        model_state = state.models[app_label, self.name_lower]
        model_state.options[self.option_name] = self.unique_together
        state.reload_model(app_label, self.name_lower)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        new_model = to_state.apps.get_model(app_label, self.name)
        if self.allow_migrate_model(schema_editor.connection.alias, new_model):
            old_model = from_state.apps.get_model(app_label, self.name)
            schema_editor.alter_unique_together(
                new_model,
                getattr(old_model._meta, self.option_name, set()),
                getattr(new_model._meta, self.option_name, set()),
            )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        return self.database_forwards(app_label, schema_editor, from_state, to_state)

    def references_field(self, model_name, name, app_label=None):
        return (
            self.references_model(model_name, app_label) and
            (
                not self.unique_together or
                any((name in together) for together in self.unique_together)
            )
        )

    def describe(self):
        return "Alter %s for %s (%s constraint(s))" % (self.option_name, self.name, len(self.unique_together or ''))


class AlterIndexTogether(FieldRelatedOptionOperation):
    """
    Changes the value of index_together to the target one.
    Input value of index_together must be a set of tuples.
    """
    option_name = "index_together"

    def __init__(self, name, index_together):
        index_together = normalize_together(index_together)
        self.index_together = set(tuple(cons) for cons in index_together)
        super(AlterIndexTogether, self).__init__(name)

    def deconstruct(self):
        kwargs = {
            'name': self.name,
            'index_together': self.index_together,
        }
        return (
            self.__class__.__name__,
            [],
            kwargs
        )

    def state_forwards(self, app_label, state):
        model_state = state.models[app_label, self.name_lower]
        model_state.options[self.option_name] = self.index_together
        state.reload_model(app_label, self.name_lower)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        new_model = to_state.apps.get_model(app_label, self.name)
        if self.allow_migrate_model(schema_editor.connection.alias, new_model):
            old_model = from_state.apps.get_model(app_label, self.name)
            schema_editor.alter_index_together(
                new_model,
                getattr(old_model._meta, self.option_name, set()),
                getattr(new_model._meta, self.option_name, set()),
            )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        return self.database_forwards(app_label, schema_editor, from_state, to_state)

    def references_field(self, model_name, name, app_label=None):
        return (
            self.references_model(model_name, app_label) and
            (
                not self.index_together or
                any((name in together) for together in self.index_together)
            )
        )

    def describe(self):
        return "Alter %s for %s (%s constraint(s))" % (self.option_name, self.name, len(self.index_together or ''))


class AlterOrderWithRespectTo(FieldRelatedOptionOperation):
    """
    Represents a change with the order_with_respect_to option.
    """

    def __init__(self, name, order_with_respect_to):
        self.order_with_respect_to = order_with_respect_to
        super(AlterOrderWithRespectTo, self).__init__(name)

    def deconstruct(self):
        kwargs = {
            'name': self.name,
            'order_with_respect_to': self.order_with_respect_to,
        }
        return (
            self.__class__.__name__,
            [],
            kwargs
        )

    def state_forwards(self, app_label, state):
        model_state = state.models[app_label, self.name_lower]
        model_state.options['order_with_respect_to'] = self.order_with_respect_to
        state.reload_model(app_label, self.name_lower)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        to_model = to_state.apps.get_model(app_label, self.name)
        if self.allow_migrate_model(schema_editor.connection.alias, to_model):
            from_model = from_state.apps.get_model(app_label, self.name)
            # Remove a field if we need to
            if from_model._meta.order_with_respect_to and not to_model._meta.order_with_respect_to:
                schema_editor.remove_field(from_model, from_model._meta.get_field("_order"))
            # Add a field if we need to (altering the column is untouched as
            # it's likely a rename)
            elif to_model._meta.order_with_respect_to and not from_model._meta.order_with_respect_to:
                field = to_model._meta.get_field("_order")
                if not field.has_default():
                    field.default = 0
                schema_editor.add_field(
                    from_model,
                    field,
                )

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        self.database_forwards(app_label, schema_editor, from_state, to_state)

    def references_field(self, model_name, name, app_label=None):
        return (
            self.references_model(model_name, app_label) and
            (
                self.order_with_respect_to is None or
                name == self.order_with_respect_to
            )
        )

    def describe(self):
        return "Set order_with_respect_to on %s to %s" % (self.name, self.order_with_respect_to)


class AlterModelOptions(ModelOptionOperation):
    """
    Sets new model options that don't directly affect the database schema
    (like verbose_name, permissions, ordering). Python code in migrations
    may still need them.
    """

    # Model options we want to compare and preserve in an AlterModelOptions op
    ALTER_OPTION_KEYS = [
        "get_latest_by",
        "managed",
        "ordering",
        "permissions",
        "default_permissions",
        "select_on_save",
        "verbose_name",
        "verbose_name_plural",
    ]

    def __init__(self, name, options):
        self.options = options
        super(AlterModelOptions, self).__init__(name)

    def deconstruct(self):
        kwargs = {
            'name': self.name,
            'options': self.options,
        }
        return (
            self.__class__.__name__,
            [],
            kwargs
        )

    def state_forwards(self, app_label, state):
        model_state = state.models[app_label, self.name_lower]
        model_state.options = dict(model_state.options)
        model_state.options.update(self.options)
        for key in self.ALTER_OPTION_KEYS:
            if key not in self.options and key in model_state.options:
                del model_state.options[key]
        state.reload_model(app_label, self.name_lower)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        pass

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        pass

    def describe(self):
        return "Change Meta options on %s" % (self.name, )


class AlterModelManagers(ModelOptionOperation):
    """
    Alters the model's managers
    """

    serialization_expand_args = ['managers']

    def __init__(self, name, managers):
        self.managers = managers
        super(AlterModelManagers, self).__init__(name)

    def deconstruct(self):
        return (
            self.__class__.__name__,
            [self.name, self.managers],
            {}
        )

    def state_forwards(self, app_label, state):
        model_state = state.models[app_label, self.name_lower]
        model_state.managers = list(self.managers)
        state.reload_model(app_label, self.name_lower)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        pass

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        pass

    def describe(self):
        return "Change managers on %s" % (self.name, )
