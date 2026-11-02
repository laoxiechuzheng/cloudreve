package model

import (
	"errors"
	"testing"

	"github.com/DATA-DOG/go-sqlmock"
	"github.com/cloudreve/Cloudreve/v3/pkg/conf"
	"github.com/cloudreve/Cloudreve/v3/pkg/hashid"
	"github.com/jinzhu/gorm"
	"github.com/stretchr/testify/assert"
)

func TestSourceLink_Link(t *testing.T) {
	a := assert.New(t)
	s := &SourceLink{}
	s.ID = 1

	// 失败
	{
		s.File.Name = string([]byte{0x7f})
		res, err := s.Link()
		a.Error(err)
		a.Empty(res)
	}

	// 成功
	{
		s.File.Name = "filename"
		res, err := s.Link()
		a.NoError(err)
		a.Contains(res, s.Name)
		a.Contains(res, "/f/"+hashid.HashID(1, hashid.SourceLinkID)+"/filename")
	}

	// 有自定义标识时使用自定义标识
	{
		s.File.Name = "filename"
		s.CustomKey = "98k-win"
		res, err := s.Link()
		a.NoError(err)
		a.Contains(res, "/f/98k-win/filename")
		a.Equal("98k-win", s.Key())
	}
}

func TestGetSourceLinkByCustomKey(t *testing.T) {
	a := assert.New(t)

	// 格式不对，不查询数据库
	{
		res, err := GetSourceLinkByCustomKey("a/b")
		a.ErrorIs(err, ErrLinkKeyInvalid)
		a.Nil(res)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 不存在
	{
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").
			WithArgs("98k-win").
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		res, err := GetSourceLinkByCustomKey("98k-win")
		a.Error(err)
		a.Nil(res)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 文件已删除
	{
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").
			WithArgs("98k-win").
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id", "custom_key"}).AddRow(3, 4, "98k-win"))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(4).WillReturnRows(sqlmock.NewRows([]string{"id"}))
		res, err := GetSourceLinkByCustomKey("98k-win")
		a.Error(err)
		a.Nil(res)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 成功
	{
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").
			WithArgs("98k-win").
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id", "custom_key"}).AddRow(3, 4, "98k-win"))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(4).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(4, "new.sh"))
		res, err := GetSourceLinkByCustomKey("98k-win")
		a.NoError(err)
		a.EqualValues(3, res.ID)
		a.Equal("new.sh", res.File.Name)
		a.NoError(mock.ExpectationsWereMet())
	}
}

func TestSourceLink_SetCustomKey(t *testing.T) {
	a := assert.New(t)
	conf.SystemConfig.HashIDSalt = ""

	// 格式不对，不查询数据库
	for _, key := range []string{"a b", "a/b", "中文"} {
		s := &SourceLink{Model: gorm.Model{ID: 2}}
		a.ErrorIs(s.SetCustomKey(key), ErrLinkKeyInvalid, key)
		a.Equal("", s.CustomKey)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 未被占用
	{
		s := &SourceLink{Model: gorm.Model{ID: 2}}
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectCommit()
		a.NoError(s.SetCustomKey("98k-win"))
		a.Equal("98k-win", s.CustomKey)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 被文件仍存在的外链占用：返回那个文件名，不写数据库
	{
		s := &SourceLink{Model: gorm.Model{ID: 2}}
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id", "custom_key"}).AddRow(7, 8, "98k-win"))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(8).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(8, "old.sh"))
		var taken LinkKeyTakenError
		a.ErrorAs(s.SetCustomKey("98k-win"), &taken)
		a.Equal("old.sh", taken.Name)
		a.Equal("", s.CustomKey)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 占用者的文件已删除：key 转给当前外链
	{
		s := &SourceLink{Model: gorm.Model{ID: 2}}
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id", "custom_key"}).AddRow(7, 8, "98k-win"))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(8).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links(.+)custom_key(.+)").
			WithArgs("", 7).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectExec("UPDATE(.+)source_links(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectCommit()
		a.NoError(s.SetCustomKey("98k-win"))
		a.Equal("98k-win", s.CustomKey)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 写入失败时回滚，不改内存中的值
	{
		s := &SourceLink{Model: gorm.Model{ID: 2}}
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").
			WithArgs("98k-win", 2).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links(.+)custom_key(.+)").
			WillReturnError(errors.New("error"))
		mock.ExpectRollback()
		a.Error(s.SetCustomKey("98k-win"))
		a.Equal("", s.CustomKey)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 是文件仍存在的外链 1 的 HashID
	{
		s := &SourceLink{Model: gorm.Model{ID: 2}}
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(1).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id"}).AddRow(1, 5))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(5).
			WillReturnRows(sqlmock.NewRows([]string{"id", "name"}).AddRow(5, "live.sh"))
		var taken LinkKeyTakenError
		a.ErrorAs(s.SetCustomKey(hashid.HashID(1, hashid.SourceLinkID)), &taken)
		a.Equal("live.sh", taken.Name)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 是文件已删除的外链 1 的 HashID：可以用
	{
		s := &SourceLink{Model: gorm.Model{ID: 2}}
		key := hashid.HashID(1, hashid.SourceLinkID)
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(1).
			WillReturnRows(sqlmock.NewRows([]string{"id", "file_id"}).AddRow(1, 5))
		mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(5).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectQuery("SELECT(.+)source_links(.+)custom_key(.+)").
			WithArgs(key, 2).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links(.+)custom_key(.+)").
			WithArgs(key, 2).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectCommit()
		a.NoError(s.SetCustomKey(key))
		a.Equal(key, s.Key())
		a.NoError(mock.ExpectationsWereMet())
	}

	// 是尚未分配的外链 ID 的 HashID：将来会被新外链抢走，不能用
	{
		s := &SourceLink{Model: gorm.Model{ID: 2}}
		mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(9).
			WillReturnRows(sqlmock.NewRows([]string{"id"}))
		mock.ExpectQuery("SELECT(.+)source_links(.+)").
			WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(3))
		a.ErrorIs(s.SetCustomKey(hashid.HashID(9, hashid.SourceLinkID)), ErrLinkKeyInvalid)
		a.Equal("", s.CustomKey)
		a.NoError(mock.ExpectationsWereMet())
	}

	// 设为自己的 HashID 等于清除，不做占用检查
	{
		s := &SourceLink{Model: gorm.Model{ID: 1}, CustomKey: "98k-win"}
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links(.+)custom_key(.+)").
			WithArgs("", 1).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectCommit()
		a.NoError(s.SetCustomKey(hashid.HashID(1, hashid.SourceLinkID)))
		a.Equal("", s.CustomKey)
		a.Equal(hashid.HashID(1, hashid.SourceLinkID), s.Key())
		a.NoError(mock.ExpectationsWereMet())
	}

	// 空值同样恢复为 HashID 链接
	{
		s := &SourceLink{Model: gorm.Model{ID: 2}, CustomKey: "98k-win"}
		mock.ExpectBegin()
		mock.ExpectExec("UPDATE(.+)source_links(.+)custom_key(.+)").
			WithArgs("", 2).
			WillReturnResult(sqlmock.NewResult(0, 1))
		mock.ExpectCommit()
		a.NoError(s.SetCustomKey(""))
		a.Equal("", s.CustomKey)
		a.NoError(mock.ExpectationsWereMet())
	}
}

func TestGetSourceLinkByID(t *testing.T) {
	a := assert.New(t)
	mock.ExpectQuery("SELECT(.+)source_links(.+)").WithArgs(1).WillReturnRows(sqlmock.NewRows([]string{"id", "file_id"}).AddRow(1, 2))
	mock.ExpectQuery("SELECT(.+)files(.+)").WithArgs(2).WillReturnRows(sqlmock.NewRows([]string{"id"}).AddRow(2))

	res, err := GetSourceLinkByID(1)
	a.NoError(err)
	a.NotNil(res)
	a.EqualValues(2, res.File.ID)
	a.NoError(mock.ExpectationsWereMet())
}

func TestSourceLink_Downloaded(t *testing.T) {
	a := assert.New(t)
	s := &SourceLink{}
	s.ID = 1
	mock.ExpectBegin()
	mock.ExpectExec("UPDATE(.+)source_links(.+)").WillReturnResult(sqlmock.NewResult(1, 1))
	mock.ExpectCommit()
	s.Downloaded()
	a.NoError(mock.ExpectationsWereMet())
}
